import json
import os
import urllib.request
import threading
from contextlib import contextmanager

from intents.registry import ARRIVAL_INTENTS
from llm.LLM_Backend import get_backend
from llm.LLM_Rag import RagIndex
from llm import Narration_Prompts
from llm.Narration_Prompts import (
    Narration, NarratorState, Notice, address_player_as_you, adam_system_message, dialogue_system_message,
    scene_query_system_message, system_message,
)
from paths import PROJECT_ROOT
from persistence.slot import FileSlotStore, SaveError


# The model's own context window is the real ceiling on a request, and it covers the prompt and
# the reply TOGETHER -- Ollama's /v1/chat/completions silently ignores num_ctx (verified against
# a live server: passing it changes neither prompt_tokens nor the cap), so the only lever this
# side of the wire is keeping the prompt small enough to leave room to answer inside it.
#
# The bug this exists to prevent: context_window's own 100-*message* cap (_queue) has
# no idea how big a message is, so a long session grew prompts to ~4000 tokens, leaving ~95 for
# the reply. Every narration came back finish_reason="length" -- silently truncated mid-sentence
# -- and roughly half the time the model spent that sliver without emitting any content at all
# and returned an empty string, which _fetch_and_publish then published as the turn's narration.
# A blank turn, from a pipeline that resolved the action perfectly well.
CONTEXT_TOKEN_BUDGET = 4096
# Held back for the reply. Measured, not guessed: an ordinary 2-3 sentence narration runs ~550
# completion tokens, and capping at 512 visibly truncated one mid-sentence, so this is that plus
# real headroom.
RESPONSE_TOKEN_RESERVE = 900
# Longest a narration waits for an earlier one to publish first (_publish_in_order). Narration
# requests carry no timeout of their own, so this is what keeps one hung request from holding
# every later narration back; a working local model answers in well under it.
PUBLISH_ORDER_TIMEOUT = 90
# The narration labels DMCore may extract scene population from -- see _fetch_and_publish. Which of
# these actually fire is a per-setting choice ([narration_population].triggers). Matched on the
# label's own kind (before any ":"), so every item_interaction:<intent> qualifies. Every
# narration that describes the scene is offered, not just arrivals: a playtest's brawler spent
# fifty turns fighting a vendor and a stranger the narrator introduced mid-scene, neither of
# whom ever became real, so nothing could target them. NPC dialogue/ADaM/scene queries stay
# out -- someone a speaker merely mentions isn't standing there.
SCENE_SETTING_LABELS = ("scenario_intro", "item_interaction", "skill_response", "clarification", "encounter")
# Deliberately a crude chars-per-token estimate rather than a real tokenizer -- the exact count
# doesn't matter when the whole point is to stay well clear of a hard ceiling, and importing a
# tokenizer here would pull a model load into a module that otherwise needs none.
CHARS_PER_TOKEN = 4


def _narrator_field(name):
    """!@brief A read/write view of one NarratorState field -- LLMCore keeps no copy of its own."""
    return property(lambda self: getattr(self.narrator, name), lambda self, value: setattr(self.narrator, name, value))


class LLMCore:
    """!
    @brief Main class for handling the local LLM. Decides nothing about what the narrator is told
        -- that's llm/Narration_Prompts.py, whose builders turn each event into a Narration (a prompt
        plus the system-message kind that frames it), a Notice or a Skip. This owns the transport:
        the rolling context window, publish ordering, sourcebook retrieval, the request itself, and
        the save slice.
    """

    # The scene knowledge the prompts read lives on self.narrator (a NarratorState); these are views.
    scenario_name = _narrator_field("scenario_name")
    scenario_description = _narrator_field("scenario_description")
    scenario_characters = _narrator_field("scenario_characters")
    population = _narrator_field("population")
    scene_name = _narrator_field("scene_name")
    exit_names = _narrator_field("exit_names")

    def __init__(self, event_bus, rag_source_dir=None, slot_store=None):
        """!
        @brief Initializes the LLM core and loads necessary models.
        @param event_bus The central event bus instance.
        @param rag_source_dir Overrides RagIndex's default Settings/Fantasy/ source directory.
        @param slot_store Where save slots live (persistence/slot.py); None is the real Saves/.
            Exists mainly so tests can point this at a directory with no PDFs (skipping the
            real sourcebook build entirely -- see RagIndex._build's early return) instead of
            every LLMCore() in the test suite kicking off a real, potentially minutes-long
            index build against whatever's actually in Settings/Fantasy/.
        """
        self.event_bus = event_bus
        self.slot_store = slot_store or FileSlotStore()
        self.event_bus.publish("log_info", "LLMCore initialized.")
        # Where requests go (local Ollama or OpenRouter) is LLM_Backend.py's get_backend(), read
        # per request -- see _request_completion.
        # Builds itself on a background thread (see RagIndex.__init__) -- perform_rag returns
        # no context at all until it's ready, rather than blocking LLMCore's own boot on
        # potentially minutes of first-time PDF extraction/embedding.
        self.rag_index = RagIndex(event_bus, source_dir=rag_source_dir)
        self.context_window = []
        # How plainly a service tagged content = "sexual" is narrated (intents/service.py's
        # CONTENT_LEVELS): "fade" by default, "explicit" only when the player chose it -- the CLI's
        # --content, or the GUI's Content menu ("content_level_selected").
        self.content_level = "fade"
        self.event_bus.subscribe("content_level_selected", self._on_content_level_selected)
        # The scenario, the cast, where the player is -- what the prompt builders read.
        self.narrator = NarratorState()
        self.event_bus.subscribe("scenario_loaded", self.generate_scene_intro)
        self.event_bus.subscribe("scene_roster_updated", self._on_scene_roster_updated)
        self.event_bus.subscribe("location_exits_updated", self._on_location_exits_updated)
        self.event_bus.subscribe("round_resolved", self.generate_round_response)
        self.event_bus.subscribe("action_resolved", self.generate_response)
        self.event_bus.subscribe("action_not_understood", self.generate_clarification_response)
        self.event_bus.subscribe("item_interaction_resolved", self.generate_item_interaction_response)
        self.event_bus.subscribe("encounter_triggered", self.generate_encounter_response)
        self.event_bus.subscribe("dialogue_resolved", self.generate_npc_dialogue)
        self.event_bus.subscribe("help_resolved", self.generate_adam_response)
        self.event_bus.subscribe("scene_query_resolved", self.generate_scene_query_response)
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)
        self.event_bus.subscribe("game_load_failed", self.generate_load_failed_response)
        self.event_bus.subscribe("crime_witnessed", self._on_crime_witnessed)
        self.event_bus.subscribe("arrest_confronted", self.generate_arrest_response)
        self.event_bus.subscribe("arrest_resolved", self.generate_arrest_response)
        # Who saw a crime this turn (DMCore's DM_Law.py) -- folded into the next narration prompt
        # so the narrator lets only them react, then cleared. See _on_crime_witnessed.
        self._crime_notes = []
        # Narrations are fetched in parallel but published in the order they were queued -- see
        # _take_publish_ticket.
        self._publish_order = threading.Condition()
        self._tickets_issued = 0
        self._next_to_publish = 0

    def _on_scene_roster_updated(self, data):
        """!
        @brief Repoints self.scenario_characters at whoever is present in the scene *now* --
            the roster system_message injects as its own " Characters: ..." line on
            every single narration.

            Before this existed that attribute was written only by generate_scene_intro (on
            scenario_loaded, once per playthrough) and load_state, so it described the
            scenario's *starting* scene forever: walk from Sandpoint's market into the tavern
            and every later narration was still told the market's cast was standing there.
            DMCore publishes this from every site that mutates scenario_entities -- see
            DM_Rules.py's _publish_scene_roster, which also owns the dirty guard that keeps
            this from firing on an unchanged scene.
        @param data The "scene_roster_updated" payload -- only "characters" is read here
            ("entities" is NLPCore's half of the same event).
        """
        self.scenario_characters = list(data.get("characters", []))
        self.population = dict(data.get("population") or self.population)
        self.scene_name = data.get("scene_name", self.scene_name)

    def _on_location_exits_updated(self, data):
        """!@brief Keeps the current location's real exits for location_rule (names only)."""
        self.exit_names = [d["name"] for d in data.get("destinations", []) if d.get("name")]


    def set_content_level(self, level):
        """!@brief Sets how plainly a sexual service is narrated; anything but "explicit" is "fade"."""
        self.content_level = "explicit" if level == "explicit" else "fade"

    def _on_content_level_selected(self, data):
        """!@brief The GUI's Content menu: {"level": "fade" | "explicit"}."""
        self.set_content_level((data or {}).get("level"))

    def set_setting(self, setting):
        """!
        @brief Repoints the RAG index at Settings/<setting>/ -- called by LLDM.py's own
            start_game right before constructing DMCore, so narration is grounded in the
            sourcebooks for whichever setting the player actually picked (GUICore's Ruleset
            menu, CLI --setting, or a loaded save's own "setting"), not whatever
            self.rag_index happened to default to at LLMCore construction time (before any
            setting was known). A no-op if the resolved source_dir hasn't actually changed
            (ex: the player picked "Fantasy", already this instance's own default, or is
            resuming a second game in the same setting) -- RagIndex._build can take minutes
            the first time, so this must never restart it needlessly.
        @param setting Which Rules/<setting> sibling under Settings/ to index (ex: "Fantasy",
            "Zombie") -- a setting with no matching Settings/<setting>/ directory (no PDFs
            authored yet, ex: "Zombie" today) just yields an empty index, the same
            "no sourcebook, no RAG context" fallback RagIndex._build already applies to a
            missing/empty source_dir.
        """
        source_dir = os.path.join(PROJECT_ROOT, "Settings", setting)
        if source_dir == self.rag_index.source_dir:
            return
        self.rag_index = RagIndex(self.event_bus, source_dir=source_dir)

    def perform_rag(self, query):
        """!
        @brief Retrieves the sourcebook passages most relevant to query, formatted for
            grounding a narration prompt. Delegates the actual embedding/matching to
            self.rag_index (see LLM_Rag.py) -- this method's only job is turning that raw
            (chunk, score) list into prompt-ready text, or "" if there's nothing to add
            (index not ready yet, or nothing cleared the confidence threshold).
        @param query The search query -- in practice, the narration prompt itself (see
            _queue), since a full prompt embeds just as well as a hand-picked
            keyword query and needs no per-call-site plumbing to construct.
        @return The retrieved context as a string, or "" if there's nothing to add -- always ""
            when the backend has sourcebook_grounding off (online, excerpts would go to a third
            party; see LLM_Backend.py).
        """
        if not get_backend().sourcebook_grounding:
            return ""
        matches = self.rag_index.query(query)
        if not matches:
            return ""
        self.event_bus.publish("log_info", f"RAG retrieved {len(matches)} chunk(s) for query.")
        return "\n".join(f"({chunk['source']} p.{chunk['page']}) {chunk['text']}" for chunk, _score in matches)

    def update_context(self, last_turn_actions, conversations):
        """!
        @brief Updates the model context with actions from the last turn and recent conversations.
        @param last_turn_actions A list of actions taken in the previous turn.
        @param conversations The recent dialogue history.
        """
        self.event_bus.publish("log_info", "Updating LLM context.")

    def generate_npc_response(self, npc_memories, npc_quotes):
        """!
        @brief Generates dialogue or actions for an NPC using their memories and quotes.
        @param npc_memories A list of the NPC's specific memories.
        @param npc_quotes A list of quotes associated with the NPC.
        @return The generated output string.
        """
        self.event_bus.publish("log_info", "Generating NPC response.")
        return ""


    def _submit(self, result):
        """!
        @brief Acts on what a Narration_Prompts builder returned: publishes its log line, then
            queues a Narration for the model, shows a Notice to the player out of character, or
            (a Skip) does nothing more.
        """
        for line in ([result.log] if isinstance(result.log, str) else result.log or []):
            self.event_bus.publish("log_info", line)
        if isinstance(result, Narration):
            self._queue(result)
        elif isinstance(result, Notice):
            self.event_bus.publish("player_notice", {"message": result.message, "reason": result.reason, "input": result.input})

    def _system_message_for(self, request, rag_query):
        """!@brief The system message framing request.kind, with sourcebook lore retrieved against rag_query."""
        rag_context = self.perform_rag(rag_query)
        if request.kind == "dialogue":
            return dialogue_system_message(request.speaker, request.persona, request.attitude, rag_context)
        if request.kind == "adam":
            return adam_system_message(request.data, rag_context)
        if request.kind == "scene_query":
            return scene_query_system_message(request.data, rag_context)
        return system_message(self.narrator, rag_context, request.label)

    def _queue(self, request):
        """!
        @brief Appends a Narration's prompt to the rolling context window (except ADaM's, which is
            a standalone, stateless request that never joins it -- its payload is gathered fresh
            from live game state every time, and left in the shared window its dense meta/OOC
            exchanges would crowd out real narrative history) and fetches the model's reply on a
            background thread, published in the order queued (_take_publish_ticket).

            A dialogue request is sent against the addressed entity's own presence-filtered view
            of the window (_filter_present_history), read live at fetch time -- another
            narration could append between queueing and fetching -- not the full window; every
            other kind sees the whole window. The exchange itself still lands in the shared
            window, tagged with present_entities, so everyone in the room (including the
            omniscient narrator) has now witnessed it.

            Who saw a crime this turn (_crime_notes) is folded into a plain narration's prompt,
            then cleared.
        @param request A Narration_Prompts.Narration.
        """
        prompt = request.prompt
        if request.kind == "narration" and self._crime_notes:
            prompt = prompt + "\n" + "\n".join(self._crime_notes)
            self._crime_notes = []
        present_entities = request.present_entities
        if request.kind != "adam":
            self.context_window.append({"role": "user", "content": prompt, "present": present_entities})

            if len(self.context_window) > 100:
                self.context_window = self.context_window[-100:]

        system = self._system_message_for(request, request.rag_query if request.rag_query else prompt)

        def fetch_from_llm():
            if request.kind == "adam":
                messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
                self._fetch_and_publish(
                    messages, present_entities=None, store_in_context=False, label=request.label, ticket=ticket,
                )
                return
            history = self._filter_present_history(request.target_key) if request.kind == "dialogue" else self.context_window
            messages = [{"role": "system", "content": system}] + self._api_messages(self._fit_history(system, history))
            self._fetch_and_publish(messages, present_entities, label=request.label, ticket=ticket, notice=request.notice)

        ticket = self._take_publish_ticket()
        threading.Thread(target=fetch_from_llm, daemon=True).start()

    def generate_scene_intro(self, data):
        """!@brief Scene intro trigger -- see Narration_Prompts.scene_intro."""
        self._submit(Narration_Prompts.scene_intro(self.narrator, data))

    def generate_round_response(self, data):
        """!@brief Round response trigger -- see Narration_Prompts.round_response."""
        self._submit(Narration_Prompts.round_response(self.narrator, data))

    def generate_response(self, data):
        """!@brief Response trigger -- see Narration_Prompts.response."""
        self._submit(Narration_Prompts.response(self.narrator, data))

    def generate_clarification_response(self, data):
        """!@brief Clarification response trigger -- see Narration_Prompts.clarification."""
        self._submit(Narration_Prompts.clarification(self.narrator, data))

    def generate_item_interaction_response(self, data):
        """!
        @brief Item interaction response trigger -- see Narration_Prompts.item_interaction. An
            arrival (a successful move/travel) first refreshes the narrator's scene state from the
            payload -- the one place it is written from an item interaction -- so this and every
            later prompt describe the place the party is in now.
        """
        if data.get("found") and data.get("intent") in ARRIVAL_INTENTS:
            self.scenario_description = data.get("room_description") or data.get("location_description", "")
            self.scenario_characters = data.get("characters", [])
        self._submit(Narration_Prompts.item_interaction(self.narrator, data))

    def generate_encounter_response(self, data):
        """!@brief Encounter response trigger -- see Narration_Prompts.encounter."""
        self._submit(Narration_Prompts.encounter(self.narrator, data))

    def generate_arrest_response(self, data):
        """!@brief Arrest response trigger -- see Narration_Prompts.arrest."""
        self._submit(Narration_Prompts.arrest(self.narrator, data))

    def generate_npc_dialogue(self, data):
        """!@brief Npc dialogue trigger -- see Narration_Prompts.npc_dialogue."""
        self._submit(Narration_Prompts.npc_dialogue(self.narrator, data))

    def generate_load_failed_response(self, data):
        """!@brief Load failed response trigger -- see Narration_Prompts.load_failed."""
        self._submit(Narration_Prompts.load_failed(self.narrator, data))

    def generate_adam_response(self, data):
        """!@brief Adam response trigger -- see Narration_Prompts.adam."""
        self._submit(Narration_Prompts.adam(self.narrator, data))

    def generate_scene_query_response(self, data):
        """!@brief Scene query response trigger -- see Narration_Prompts.scene_query."""
        self._submit(Narration_Prompts.scene_query(self.narrator, data))

    @staticmethod
    def _api_messages(entries):
        """!
        @brief Projects context_window entries down to the bare {"role", "content"} shape the
            chat-completions API actually expects, stripping the "present" bookkeeping tag
            (see _queue's own present_entities param) that never leaves this process
            -- it's local presence metadata for _filter_present_history, not something Ollama
            has any use for.
        @param entries A list of context_window-shaped entries.
        @return The same entries, each reduced to just role/content.
        """
        return [{"role": entry["role"], "content": entry["content"]} for entry in entries]

    def _filter_present_history(self, entity_name):
        """!
        @brief The subset of context_window entity_name was actually present for -- room-level
            presence (see _queue's present_entities param), not the DM's own always-
            full context_window. An entry with no "present" tag at all (ex:
            generate_clarification_response/generate_load_failed_response, neither of which
            DMCore is involved in producing, so there's no scenario_entities to tag them with)
            is excluded rather than assumed witnessed, the same for any entry from before
            entity_name existed or was ever in the same room as whatever it describes. This is
            what generate_npc_dialogue grounds a specific NPC's own reply in, instead of the
            omniscient window every other narration trigger reads from.
        @param entity_name The entity whose own witnessed history is being built.
        @return The ordered subset of context_window entries entity_name's own "present" tag
                includes -- same relative order and the same 100-message ceiling context_window
                itself already enforces; no separate cap here.
        """
        return [entry for entry in self.context_window if entity_name in (entry.get("present") or ())]

    @property
    def model(self):
        """!@brief The current backend's primary model (LLM_Backend.py)."""
        return get_backend().model

    def _request_completion(self, data):
        """!
        @brief One POST to the current backend (LLM_Backend.py), returning the model's own reply text. An empty string is a
            real thing a local model returns rather than an error case, so it comes back as-is
            for the caller to decide about; only transport/decode failures raise.
        @param data The request body's fields, "messages" included -- the backend adds its model.
        @return The reply content, "" included.
        """
        backend = get_backend()
        req = urllib.request.Request(
            backend.api_url,
            data=json.dumps(backend.payload(**data)).encode('utf-8'),
            headers=backend.headers(),
        )
        response = urllib.request.urlopen(req)
        result = json.loads(response.read().decode('utf-8'))
        served = result.get("model")
        if backend.fallback_models and served and served != backend.model:
            # OpenRouter moved on down the list (see LLM_Backend.py's OPENROUTER_FREE_MODELS).
            self.event_bus.publish("log_info", f"LLM reply came from fallback model {served}.")
        return result['choices'][0]['message']['content'] or ""

    def _fit_history(self, system_message, history):
        """!
        @brief Drops the oldest history entries until the system message plus what's left
            leaves RESPONSE_TOKEN_RESERVE worth of room for the reply inside the model's own
            context window (see this module's CONTEXT_TOKEN_BUDGET note for the truncation/
            empty-narration bug this exists to prevent).

            This is a separate axis from context_window's own 100-message cap, not a
            replacement for it: that one bounds what the game REMEMBERS, this one bounds what
            any single request SENDS. A long scene keeps its full remembered history for
            _filter_present_history and later turns; it just stops trying to put all of it on
            the wire at once.

            The newest entries are kept, oldest dropped, since recent turns ground the current
            moment. At least one entry always survives even if it alone blows the budget --
            sending the prompt that actually prompted this turn and letting the model truncate
            beats sending a bare system message with no player action in it at all.
        @param system_message The system message this request will carry, counted against the
            same budget.
        @param history The context_window-shaped entries to fit.
        @return The kept entries, oldest-first, ready for _api_messages.
        """
        allowance = (CONTEXT_TOKEN_BUDGET - RESPONSE_TOKEN_RESERVE) * CHARS_PER_TOKEN - len(system_message)
        kept = []
        used = 0
        for entry in reversed(history):
            cost = len(entry.get("content") or "")
            if kept and used + cost > allowance:
                break
            used += cost
            kept.append(entry)
        kept.reverse()
        return kept

    def _take_publish_ticket(self):
        """!
        @brief A place in the publishing order, taken on the game thread when a narration is
            queued. Each fetch runs on its own thread, and a short prompt can come back before a
            long one queued earlier -- found by playtest: a guard's arrest demand was narrated
            before the attack it was about. _publish_in_order holds each reply until every
            earlier ticket has published.
        """
        with self._publish_order:
            ticket = self._tickets_issued
            self._tickets_issued += 1
            return ticket

    @contextmanager
    def _publish_in_order(self, ticket):
        """!
        @brief Waits until every earlier ticket has published, then lets this one publish. A
            reply that never comes back can't hold the rest up for longer than
            PUBLISH_ORDER_TIMEOUT. ticket None (a direct, unqueued call) publishes at once.
        """
        if ticket is None:
            yield
            return
        with self._publish_order:
            self._publish_order.wait_for(lambda: self._next_to_publish >= ticket, timeout=PUBLISH_ORDER_TIMEOUT)
        try:
            yield
        finally:
            with self._publish_order:
                self._next_to_publish = max(self._next_to_publish, ticket + 1)
                self._publish_order.notify_all()

    def _publish_attached_notice(self, notice):
        """!@brief A narration's attached out-of-character line (see _fetch_and_publish)."""
        if notice:
            self.event_bus.publish("player_notice", {"message": notice, "reason": "attached", "input": ""})

    def _fetch_and_publish(self, messages, present_entities, store_in_context=True, label=None, ticket=None, notice=None):
        """!
        @brief The network call + response handling shared by _queue/_queue/
            _queue/_queue's own background fetch threads --
            everything downstream of "here are the messages to send" is identical either way:
            POST to Ollama, optionally append the reply to the shared context_window (tagged
            with present_entities, same as the prompt that prompted it, so it becomes part of
            what everyone present has now witnessed), and publish llm_response_ready/
            llm_debug_updated. Must never raise -- runs on a background thread with nothing to
            catch an exception it doesn't handle itself (see this file's own module note on
            LLM_Client.py's different contract).
        @param messages The complete [{"role", "content"}, ...] list to send, system message
            included.
        @param present_entities The presence tag to attach to the appended assistant turn.
        @param store_in_context Whether to append the reply to context_window at all -- True
            for every ordinary narration/dialogue trigger, False for _queue,
            whose exchanges are deliberately excluded from the shared window entirely (see
            _queue's own docstring for why).
        @param label A short tag identifying which narration trigger this request came from
            (ex: "dialogue:town crier", "item_interaction:advance", "scenario_intro") --
            forwarded from whichever generate_*/_queue_* call site kicked this off (each
            already logs its own "Generating ..." log_info line; this is that same context,
            just carried onto the request/response pair itself). Threaded through to
            "llm_debug_updated" so Logger.py's own DEBUG file (see LLDM.py's DEBUG flag) can
            print it right on the QUERY/RESPONSE block -- without this, telling two
            near-simultaneous background calls' own query/response pairs apart (ex: a scene's
            own automatic intro narration landing right next to an NPC's reply to the player,
            purely because Ollama serializes requests) required manually reading each query's
            own prompt text and cross-referencing timestamps against "Processing player input"/
            "Generating ..." log lines by hand -- exactly the confusion that first looked like
            two replies to one query but wasn't. None (unlabeled call sites, ex: a bare
            _fetch_and_publish caller that predates this) just omits the tag.
        @param ticket This narration's place in the publishing order (_take_publish_ticket);
            None publishes as soon as the reply arrives.
        @param notice A "player_notice" message published right after the narration, in the
            same slot -- and still published if the narration failed, since the player needs it
            either way.
        """
        data = {"messages": messages, "temperature": 0.7,
                "max_tokens": RESPONSE_TOKEN_RESERVE}
        # Exactly what's about to go over the wire, formatted for a human -- see
        # display_llm_debug (GUI_Core.py)'s Debug tab, not narration itself.
        query_text = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
        try:
            llm_text = self._request_completion(data)
            if not llm_text.strip():
                # Belt and suspenders behind _fit_history's own budget: an empty completion is
                # overwhelmingly a starved-context symptom, but it costs one extra call to rule
                # out a one-off rather than hand the player a blank turn.
                self.event_bus.publish("log_warning", "LLM returned an empty response; retrying once.")
                llm_text = self._request_completion(data)
            if not llm_text.strip() and len(messages) > 2:
                # Found by playtest: two instant empty replies on a 14 KB request -- not a
                # starved context, just a request this model keeps ending at once, which an
                # identical retry repeats. A last try without the history (system message and
                # this turn's own prompt only) asks it something different enough to answer.
                self.event_bus.publish("log_warning", "LLM returned an empty response again; retrying without history.")
                llm_text = self._request_completion({**data, "messages": [messages[0], messages[-1]]})
        except Exception as e:
            with self._publish_in_order(ticket):
                self.event_bus.publish("log_error", f"LLM connection failed: {e}")
                self.event_bus.publish("llm_response_ready", f"System: {get_backend().failure_message(e)}")
                self.event_bus.publish("llm_debug_updated", {"query": query_text, "response": f"[ERROR] {e}", "label": label})
                self._publish_attached_notice(notice)
            return
        with self._publish_in_order(ticket):
            if not llm_text.strip():
                # Deliberately NOT stored in context_window -- an empty assistant turn is not
                # something the scene witnessed, and keeping it would spend budget on nothing
                # and teach the model that empty replies belong here.
                self.event_bus.publish("log_error", "LLM returned an empty response twice; nothing to narrate this turn.")
                self.event_bus.publish("llm_response_ready", "System: The LLM returned an empty response.")
                self.event_bus.publish("llm_debug_updated", {"query": query_text, "response": "[EMPTY]", "label": label})
                self._publish_attached_notice(notice)
                return
            # Before it's stored, so a slip never reaches the history the next reply imitates.
            llm_text = address_player_as_you(llm_text)
            if store_in_context:
                self.context_window.append({"role": "assistant", "content": llm_text, "present": present_entities})
            self.event_bus.publish("llm_response_ready", llm_text)
            self.event_bus.publish("llm_debug_updated", {"query": query_text, "response": llm_text, "label": label})
            self._publish_attached_notice(notice)
        if label and label.split(":")[0] in SCENE_SETTING_LABELS:
            # After the narration is already on screen, so extraction (a second, slower model
            # call) overlaps with the player reading it -- see scene_length_instruction. Outside
            # the publishing order, so it never holds up the next narration.
            self.event_bus.publish("scene_narration_ready", {
                "text": llm_text, "label": label, "present_entities": present_entities,
            })

    def _on_crime_witnessed(self, data):
        """!
        @brief Notes who saw a crime, for the next narration prompt. Who knows is game state
            (DMCore's known_crimes), so the narrator is told exactly that, rather than left to
            decide a passing stranger noticed too.
        @param data The "crime_witnessed" payload ({"crime", "offender", "victim", "witnesses"}).
        """
        witnesses = ", ".join(data.get("witnesses") or [])
        if not witnesses:
            return
        self._crime_notes.append(
            f"Seen by: {witnesses}. Nobody else present noticed -- only they may react to it."
        )


    def _save_slot_dir(self, slot_name):
        """!
        @return This slot's directory -- the slot store's own answer, shared with DMCore/GUICore.
        """
        return self.slot_store.slot_dir(slot_name)

    def save_game(self, slot_name):
        """!
        @brief Writes this core's own part of a save slot -- the rolling narration
            context_window plus scenario bookkeeping -- as "llm_state". DMCore independently
            writes its own sibling part for the same slot (see docs/persistence.md for why
            this isn't one combined file).
        @param slot_name The save slot's name.
        """
        self.slot_store.write(slot_name, "llm_state", {
            "context_window": self.context_window,
            "scenario_name": self.scenario_name,
            "scenario_description": self.scenario_description,
            "scenario_characters": self.scenario_characters,
        })
        self.event_bus.publish("log_info", f"LLM narration state saved to slot '{slot_name}'.")

    def load_game(self, slot_name):
        """!
        @brief Restores context_window/scenario bookkeeping from the slot's "llm_state" part,
            silently -- no LLM call, no new narration -- so resuming a session doesn't reprint an
            opening-scene intro the way a genuine "scenario_loaded" would. A part that can't be
            read just logs and leaves current state alone; DMCore's own load_game is what
            publishes "game_load_failed" for narrating that to the player (see
            generate_load_failed_response), so this doesn't duplicate that feedback.
        @param slot_name The save slot's name to load.
        """
        try:
            data = self.slot_store.read(slot_name, "llm_state")
        except SaveError as error:
            self.event_bus.publish("log_error", f"No LLM narration state for slot '{slot_name}' ({error}).")
            return

        self.context_window = data.get("context_window", [])
        self.scenario_name = data.get("scenario_name", "")
        self.scenario_description = data.get("scenario_description", "")
        self.scenario_characters = data.get("scenario_characters", [])
        self.event_bus.publish("log_info", f"LLM narration state loaded from slot '{slot_name}'.")

    def _on_save_requested(self, data):
        """!
        @brief Event handler for a save request (from NLPCore's text intercept or a GUI/Textual
            button, both publishing the same event as DMCore's own handler).
        @param data The "save_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "save_requested with no slot name; ignored.")
            return
        self.save_game(slot_name)

    def _on_load_requested(self, data):
        """!
        @brief Event handler for a load request, mirroring _on_save_requested.
        @param data The "load_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "load_requested with no slot name; ignored.")
            return
        self.load_game(slot_name)
