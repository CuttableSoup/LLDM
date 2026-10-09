import re

from dm.DM_Types import DMCoreProtocol
import resolution.Combat_Resolution as Combat_Resolution
import resolution.Social_Resolution as Social_Resolution
from resolution.Action_Target import FEMALE_GENDERS, MALE_GENDERS
from resolution.Entity_Reference import first_named, mentions

# How many turn-costing, non-dialogue turns a conversation survives before it lapses -- long
# enough to hand over a coin or glance around mid-talk, short enough that wandering off to pick
# a lock doesn't leave the last shopkeeper answering every stray remark.
CONVERSATION_IDLE_TURNS = 3


class DialogueMixin(DMCoreProtocol):
    """!
    @brief Direct, in-character address of a specific present entity (DMCore mixin -- only
        ever composed into DMCore, never instantiated on its own; relies on
        self.entities/self.scenario_entities/self.player_name/self.event_bus, set up by
        DMCore.__init__). Inherits DMCoreProtocol purely so type checkers can resolve these
        shared attributes/cross-mixin methods -- see DM_Types.py.

        This is a genuinely different channel from a skill-based social check (persuade/
        intimidate/deceive, still resolved the ordinary dice way through resolve_opposed_action
        and narrated in third person by the omniscient Game Master -- see DM_Core.py's own
        "Action resolution pipeline"). Free-form talking/asking never rolls dice at all, the
        same "conversational, non-mechanical bypasses dice" rule "Items and movement as
        intents" already applies to examine/give/trade/formation -- this is a third such
        channel, not a fourteenth item-interaction intent, since its shape (no item, an
        addressee resolved from the scene itself, a generated in-character reply rather than a
        structured mechanical outcome) doesn't fit item_interaction_detected's dispatcher at
        all. See NLP_Core.py's DIALOGUE_KEYWORDS for what triggers this.
    """

    def _literal_dialogue_target(self, input_text):
        """!
        @brief The literal half of _resolve_dialogue_target: a whole-word, case-insensitive
            search of input_text for any currently-in-scene entity (excluding the player) --
            the same "DMCore, not NLPCore, decides who's named" approach
            DM_Movement.py's _resolve_formation_intent already uses for party positioning,
            generalized here to every scenario entity, not just party members, since a
            dialogue partner can be any NPC or creature present, not only an ally. Declaration
            order in self.scenario_entities breaks a tie the same way every other
            first-match-wins list in this codebase already does.

            Three phrases per entity rather than one: its self.entities *key*, its own "name"
            field, and each entry of an optional "aliases" list. The key alone became a real
            gap the moment instanced crowds existed -- a background instance keyed
            "sandpoint_townsfolk_2" but displayed to the player as "Fishmonger" was
            unaddressable by the only name the player ever sees. "aliases" is the same
            mechanism [[location.exit]] already uses for "the tavern" reaching "The White Deer
            Tavern and Inn", applied to people: it's what lets "greet the barkeep" reach
            Garridan Viskalai without anyone having to author that phrasing as a keyword.

            Split out from _resolve_dialogue_target specifically so the promotion gate can ask
            the unambiguous question "did the player name someone who is actually here?"
            without the default-target fallback masking the answer -- see DM_Core.py's
            _on_dialogue_detected.
        @param input_text The player's raw (already lowercased) input.
        @return The addressed entity's name, or None if nothing present is named at all.
        """
        return first_named(input_text, self.entities, self.scenario_entities, exclude=self.player_name)

    def _set_conversation_partner(self, target_name):
        """!
        @brief Records target_name as who the player is talking to, and restarts its idle
            count. Publishes "conversation_partner_updated" only when the partner actually
            changes -- NLPCore keeps its own copy (IntentClassifier.set_conversation_partner)
            to decide whether an unmarked line is speech at all.
        @param target_name The entity key just spoken to, or None to end the conversation.
        """
        current = (self.conversation_partner or {}).get("key")
        self.conversation_partner = {"key": target_name, "idle_turns": 0} if target_name else None
        if target_name != current:
            self._publish_conversation_partner()

    def _publish_conversation_partner(self):
        """!
        @brief Publishes the current conversation partner (or None) for NLPCore -- called on
            every change and once after load_game, which restores the field directly.
        """
        partner = None
        if self.conversation_partner:
            key = self.conversation_partner["key"]
            entity = self.entities.get(key, {})
            partner = {"key": key, "name": entity.get("name", key), "aliases": list(entity.get("aliases", []))}
        self.event_bus.publish("conversation_partner_updated", {"partner": partner})

    def _current_conversation_partner(self):
        """!
        @brief The conversation partner's key if they can still hear the player -- present,
            alive, and not hidden, the same gates _resolve_dialogue applies -- else None, ending
            the conversation as a side effect. Checked lazily, wherever the partner is read,
            rather than hooked into every site that can kill, hide, or move an entity.
        """
        key = (self.conversation_partner or {}).get("key")
        if not key:
            return None
        if key not in self.scenario_entities or Combat_Resolution.get_current_hp(self.world, key) <= 0 or self.is_hidden(key):
            self._set_conversation_partner(None)
            return None
        return key

    def _tick_conversation_partner(self):
        """!
        @brief Counts one turn-costing turn that wasn't dialogue against the current
            conversation, ending it after CONVERSATION_IDLE_TURNS. Called from DMCore's
            _on_turn_detected; dialogue itself resets the count via _set_conversation_partner.
        """
        if not self._current_conversation_partner():
            return
        self.conversation_partner["idle_turns"] += 1
        if self.conversation_partner["idle_turns"] >= CONVERSATION_IDLE_TURNS:
            self._set_conversation_partner(None)

    def _resolve_dialogue_target(self, input_text):
        """!
        @brief Figures out who's being addressed: whoever _literal_dialogue_target (above)
            names, falling back to
            _get_target_name()'s own default scene target (the first non-party entity present)
            if no name is found in the input at all, the same default every item-interaction
            intent already falls back to -- so a bare "ask about the weather" still addresses
            whoever's obviously being talked to in a two-person scene. Deliberately the one
            call site passing include_background=True (see DM_Core.py's _get_target_name): an
            ambient crowd member is exactly who an unaddressed remark in a market square should
            land on, even though the same entity must never become the default "open it" target.
        @param input_text The player's raw (already lowercased) input.
        @return The addressed entity's name, or None if nothing named matches and there's no
                default target either (ex: an empty scene).
        """
        return (
            self._literal_dialogue_target(input_text)
            or self._current_conversation_partner()
            or self._default_listener()
        )

    def _default_listener(self):
        """!
        @brief Who an unnamed remark reaches with no conversation running: the first person
            present who understands the player's current language, else _get_target_name's own
            first non-object. Found by playtest: the first person in a market scene spoke only
            another tongue, so 80 turns of unnamed talk all came back as gibberish while
            neighbors who shared the player's language stood by.
        @return An entity key, or None in an empty scene.
        """
        for name in self.scenario_entities:
            entity = self.entities.get(name, {})
            if (
                self._is_party_member(name) or entity.get("supertype") == "object"
                or Combat_Resolution.get_current_hp(self.world, name) <= 0 or self.is_hidden(name)
            ):
                continue
            if self._detect_language_barrier(name)[0] is None:
                return name
        fallback = self._get_target_name(include_background=True, include_objects=False)
        # Never the dead: found by playtest, an unnamed remark kept going to the corpse of a
        # bystander killed the turn before.
        return fallback if fallback and Combat_Resolution.get_current_hp(self.world, fallback) > 0 else None

    def _resolve_dialogue(self, input_text, sentiments=None, forced_target=None):
        """!
        @brief Resolves a dialogue attempt against whoever _resolve_dialogue_target names:
            gated on actually being present (in self.scenario_entities right now -- a room-
            local NPC left behind in a previous room of a multi-room dungeon doesn't qualify),
            alive, noticed (not is_hidden -- same "can't address what you haven't spotted yet"
            rule _attach_defender_details already follows), and not an inanimate "object"
            supertype (a chest has nothing to say). Deliberately does *not* gate on hostility
            at all -- unlike combat targeting, addressing a hostile entity is allowed (shouting
            a question mid-fight, taunting, demanding a wolf back off); whatever the model
            produces for a hostile target is free to read as dismissive or aggressive in
            character, but the attempt itself is never denied for it.
        @param input_text The player's raw (already lowercased) input.
        @param forced_target An addressee to use instead of running _resolve_dialogue_target
            at all -- passed only by DMCore's own _on_dialogue_detected after it has just
            materialized this entity into the scene (see
            ImprovisationMixin._attempt_dialogue_promotion). It deliberately bypasses only the
            *resolution* step, not the gates below: a promoted NPC is a live, present, alive,
            visible, non-object entity, so it passes them all on its own merits and flows into
            the ordinary found=True path. That is what makes a promoted turn produce one
            coherent in-character reply rather than a denial followed by a second narration.
        @param sentiments {axis_name: (label, score)} -- NLPCore's own local classification of
            input_text's tone, one entry per attitude axis (disposition/threat/familiarity),
            applied via nudge_attitude (SocialMixin, DM_Social.py) before persona/attitude are
            read, so a found target's own attitude description already reflects this turn's
            drift. Only ever applied on a found target -- there's nothing to nudge if no one's
            actually listening. Never applied (and sentiments are simply ignored) when
            _detect_language_barrier finds no shared tongue -- target never understood the
            words well enough for their tone to register.
        @return {"target", "found"} plus, on success, {"persona", "attitude"} (see
                describe_character/describe_attitude, SocialMixin) for LLMCore to speak from --
                or, if _detect_language_barrier finds no language in common, also
                {"language_barrier": True, "target_language", "nonsense_phrase"} instead of
                applying sentiments at all; on failure, {"reason"} instead ("no_one_here" if
                nothing could be resolved at all, "dead" if they're here but dead, "not_present" if
                the resolved name isn't currently here/noticed, "cant_talk" if it's an inanimate
                object).
        """
        # Resolution method captured alongside target_name itself (rather than just calling
        # _resolve_dialogue_target and re-deriving it after the fact) specifically so it can be
        # logged below -- without this, a case like "the player typed a real name, but nothing
        # in the current scene actually answers to it anymore (ex: a background NPC re-rolled
        # to a different display name since the input was typed against an older roster) so the
        # literal scan misses and this silently falls back to whoever's default" was invisible
        # in the logs and had to be reconstructed by hand from unrelated "Placed background
        # NPC"/system-message roster lines.
        if forced_target:
            target_name = forced_target
            resolution = "promoted -- just materialized into the scene on reference"
        else:
            target_name = self._literal_dialogue_target(input_text)
            if target_name:
                resolution = "literal match -- named by key/display name/alias in the input"
            elif self._current_conversation_partner():
                target_name = self._current_conversation_partner()
                resolution = "conversation partner -- no name in the input, still talking to them"
            else:
                target_name = self._default_listener()
                resolution = "fallback default -- no name matched in the input" if target_name else None

        if not target_name:
            self.event_bus.publish(
                "log_info", "Resolved dialogue target: none -- nothing present to fall back to.",
            )
            return {"target": None, "found": False, "reason": "no_one_here"}
        self.event_bus.publish("log_info", f"Resolved dialogue target: '{target_name}' ({resolution}).")

        if target_name in self.scenario_entities and Combat_Resolution.get_current_hp(self.world, target_name) <= 0:
            # Said outright: told only "isn't here to respond", the narrator had a corpse
            # "gasping for air" through thirteen turns of a playtest.
            return {"target": target_name, "found": False, "reason": "dead"}
        if target_name not in self.scenario_entities or self.is_hidden(target_name):
            return {"target": target_name, "found": False, "reason": "not_present"}

        if self.entities.get(target_name, {}).get("supertype") == "object":
            return {"target": target_name, "found": False, "reason": "cant_talk"}

        # Talking to them at all -- understood or not -- is what makes them the partner the
        # next unmarked line goes to.
        self._set_conversation_partner(target_name)

        barrier_language, nonsense_phrase = self._detect_language_barrier(target_name)
        if barrier_language:
            # Deliberately skips nudge_attitude below: the sentiment classifiers read the
            # *meaning* of what the player said, which target never actually understood --
            # only persona/attitude (tone, not words) still ground this reply.
            return {
                "target": target_name,
                "found": True,
                "language_barrier": True,
                "target_language": barrier_language,
                "nonsense_phrase": nonsense_phrase,
                "persona": self.describe_character(target_name),
                "attitude": self.describe_attitude(target_name, self.player_name),
            }

        self.nudge_attitude(target_name, self.player_name, sentiments or {})

        return {
            "target": target_name,
            "found": True,
            "persona": self.describe_character(target_name),
            "attitude": self.describe_attitude(target_name, self.player_name),
        }

    def _current_language(self):
        """!
        @brief The player's own currently-spoken language -- a persistent, single-language
            choice rather than "all known languages at once" (see _detect_language_barrier's
            own docstring for the gap this closes). Absent entity field defaults to the first
            entry of the player's own "languages" list (chargen's own ordering: "common" first,
            the chosen race's language appended after -- DM_CharacterCreation.py's
            apply_character_creation), computed fresh every call rather than eagerly written at
            chargen -- same "absent means the quiet default applies" convention every other
            optional entity field already follows (ex: armor_tags).
        @return The player's currently-active language name.
        """
        player = self.entities.get(self.player_name, {})
        return player.get("current_language") or (player.get("languages") or ["common"])[0]

    def _shares_language_with(self, target_name):
        """!
        @brief Whether target_name understands the player (see _detect_language_barrier) --
            a plain bool wrapper around _detect_language_barrier for callers (ex: Combat_Actions.py's
            _ability_requires_language gate) that only need a yes/no, not the narration-facing
            target_language/nonsense_phrase pair.
        @param target_name The entity being checked against.
        @return True if target_name understands the player.
        """
        return self._detect_language_barrier(target_name)[0] is None

    def _resolve_language_intent(self, input_text, resolved):
        """!
        @brief Handles "speak_language" -- switching which of the player's own known languages
            is currently active (see _current_language), the same "search the raw input for a
            known name" pattern _resolve_formation_intent already uses for a party member's own
            name, here searched against the player's own "languages" list instead. A player
            naming a language they don't actually know (or naming nothing recognizable at all)
            is declined outright rather than guessed at -- there's nothing sensible to switch to.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        known = self.entities.get(self.player_name, {}).get("languages") or ["common"]
        named = [
            language for language in known
            if mentions(input_text, language)
        ]
        if not named:
            resolved(False, reason="unknown_language")
            return

        self.entities[self.player_name]["current_language"] = named[0]
        resolved(True, language=named[0])

    def _gesture_target(self, input_text):
        """!
        @brief Who a wordless gesture is aimed at: whoever the input names, else the conversation
            partner, else the one person a gendered pronoun can only mean, else the one other person
            in the scene if there is exactly one -- and no one otherwise. Deliberately not _resolve_dialogue_target's default-listener fallback: a
            remark can land on whoever is nearest, but a kiss or a bow must not.
        @param input_text The player's raw (already lowercased) input.
        @return An entity key, or None.
        """
        named = self._literal_dialogue_target(input_text)
        if named:
            return named
        partner = self._current_conversation_partner()
        if partner:
            return partner
        # "pull him into a kiss": a gendered pronoun means the one person present it can only mean
        # (the same rule an attack's pronoun follows -- two men present and "him" means nobody).
        words = set(re.findall(r"[a-z]+", input_text or ""))
        wanted = (
            FEMALE_GENDERS if words & {"her", "she", "hers"}
            else MALE_GENDERS if words & {"him", "his", "he"} else None
        )
        if wanted:
            matches = [
                name for name in self.scenario_entities
                if name != self.player_name and not self._is_party_member(name)
                and self.entities.get(name, {}).get("supertype") == "creature"
                and Combat_Resolution.get_current_hp(self.world, name) > 0 and not self.is_hidden(name)
                and str((self.entities[name].get("qualities") or {}).get("gender", "")).lower() in wanted
            ]
            if len(matches) == 1:
                return matches[0]
        others = [
            name for name in self.scenario_entities
            if name != self.player_name and not self._is_party_member(name)
            and self.entities.get(name, {}).get("supertype") != "object"
            and Combat_Resolution.get_current_hp(self.world, name) > 0 and not self.is_hidden(name)
        ]
        return others[0] if len(others) == 1 else None

    def _resolve_gesture_intent(self, input_text, tone, resolved):
        """!
        @brief Handles "gesture" -- a wordless expressive act (a kiss, a bow, a dance) the
            adjudicator classified (AdHoc_Generation.py's adjudicate_player_input), whose tone is
            one the setting authors as an [[attitude_event]] "tone". Diceless. Only the target's
            attitude moves, through that event (or its "unwelcome_event" when they already feel
            below the event's "unwelcome_below" toward the player) -- never a bystander's. The
            language barrier does not apply: a bow needs no shared tongue. An object, or no one
            at all, is simply a gesture at nothing: narrated, moving no attitude. A dead or
            absent named target is declined, so the narrator doesn't invent a reaction.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @param tone One of Social_Resolution.gesture_tones, as the adjudicator named it.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        target_name = self._gesture_target(input_text)
        entity = self.entities.get(target_name, {}) if target_name else {}
        if target_name and entity.get("supertype") == "object":
            target_name = None
        if target_name:
            if target_name in self.scenario_entities and Combat_Resolution.get_current_hp(self.world, target_name) <= 0:
                resolved(False, reason="dead", target=target_name, tone=tone)
                return
            if target_name not in self.scenario_entities or self.is_hidden(target_name):
                resolved(False, reason="not_present", target=target_name, tone=tone)
                return

        if not target_name:
            resolved(True, tone=tone, target=None)
            return

        event_name, unwelcome = Social_Resolution.gesture_event_name(
            self.rules, tone, self.get_attitude(target_name, self.player_name)[0],
        )
        if event_name:
            self.nudge_attitude_from_event(target_name, self.player_name, event_name, 1.0)
        # Aiming a gesture at someone is addressing them: the next unmarked line goes to them.
        self._set_conversation_partner(target_name)
        resolved(
            True, tone=tone, target=target_name, target_label=entity.get("name", target_name),
            persona=self.describe_character(target_name),
            attitude=self.describe_attitude(target_name, self.player_name), unwelcome=unwelcome,
        )

    def _detect_language_barrier(self, target_name):
        """!
        @brief Whether the player is understood by target_name at all: in the language they
            explicitly chose (current_language, a single persistent choice set only by
            _resolve_language_intent), or, with no choice made, in any language they know.
            Compares against target_name's own full "languages" list (an entity field,
            entity_schema.toml; absent entirely defaults to ["common"], same as every entity
            shipped today, so this never fires against existing data unless an author
            deliberately narrows an entity's own list, or the player knows none of that entity's
            languages (or chose one it doesn't know) -- see races.toml's own "language" field and
            DM_CharacterCreation.py's apply_character_creation for how a chosen race's language
            lands on the player). Deliberately asymmetric: only the player's side is ever
            narrowed to one active tongue -- a target's own multiple known languages all still
            count toward whether *it* understands the player, since there's no equivalent
            "which one is it currently speaking" ambiguity on that side.
        @param target_name The addressed entity, already confirmed present/alive/animate.
        @return (None, None) if a language is shared. Otherwise
                (target_language, nonsense_phrase): target_language is the first of target's
                own unshared languages (what a narration prompt names as "the language it
                spoke"), nonsense_phrase is whichever race in races.toml claims that language
                as its own "language" field (see get_race), or None if no race does (ex: a
                scenario-authored language with no matching race entry) -- LLM_Core.py's own
                language-barrier prompt still works without one, just with no style example to
                draw from.
        """
        player = self.entities.get(self.player_name, {})
        # Only a language the player explicitly chose ("speak in elvish") narrows them to one
        # tongue. Otherwise they talk in whichever of theirs the listener knows -- found by
        # playtest: a Varisian-first default character heard nothing but gibberish from every
        # NPC left on the "common" default, which no ordering of their own list could fix.
        spoken = [player["current_language"]] if player.get("current_language") else (player.get("languages") or ["common"])
        target_languages = self.entities.get(target_name, {}).get("languages") or ["common"]

        if any(language in target_languages for language in spoken):
            return None, None

        target_language = target_languages[0]
        nonsense_phrase = None
        for race in self.rules.get("race", []):
            if race.get("language") == target_language:
                nonsense_phrase = race.get("nonsense_phrase")
                break

        return target_language, nonsense_phrase
