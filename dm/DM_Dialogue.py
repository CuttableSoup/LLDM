import re

from dm.DM_Types import DMCoreProtocol


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

    def _resolve_dialogue_target(self, input_text):
        """!
        @brief Figures out who's being addressed: a literal, whole-word, case-insensitive
            search of input_text for any currently-in-scene entity's own name (excluding the
            player) -- the same "DMCore, not NLPCore, decides who's named" approach
            DM_Movement.py's _resolve_formation_intent already uses for party positioning,
            generalized here to every scenario entity, not just party members, since a
            dialogue partner can be any NPC or creature present, not only an ally. Declaration
            order in self.scenario_entities breaks a tie the same way every other
            first-match-wins list in this codebase already does. Falls back to
            _get_target_name()'s own default scene target (the first non-party entity present)
            if no name is found in the input at all, the same default every item-interaction
            intent already falls back to -- so a bare "ask about the weather" still addresses
            whoever's obviously being talked to in a two-person scene.
        @param input_text The player's raw (already lowercased) input.
        @return The addressed entity's name, or None if nothing named matches and there's no
                default target either (ex: an empty scene).
        """
        named = [
            name for name in self.scenario_entities
            if name != self.player_name and re.search(rf"\b{re.escape(name.lower())}\b", input_text or "")
        ]
        if named:
            return named[0]
        return self._get_target_name()

    def _resolve_dialogue(self, input_text, sentiments=None):
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
                nothing could be resolved at all, "not_present" if the resolved name isn't
                currently here/alive/noticed, "cant_talk" if it's an inanimate object).
        """
        target_name = self._resolve_dialogue_target(input_text)
        if not target_name:
            return {"target": None, "found": False, "reason": "no_one_here"}

        if (
            target_name not in self.scenario_entities
            or self.get_current_hp(target_name) <= 0
            or self.is_hidden(target_name)
        ):
            return {"target": target_name, "found": False, "reason": "not_present"}

        if self.entities.get(target_name, {}).get("supertype") == "object":
            return {"target": target_name, "found": False, "reason": "cant_talk"}

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

    def _detect_language_barrier(self, target_name):
        """!
        @brief Whether the player and target_name share no language at all -- both entities'
            own "languages" list (an entity field, entity_schema.toml; absent entirely defaults
            to ["common"], same as every entity shipped today, so this never fires against
            existing data unless an author deliberately narrows an entity's own list, or the
            player picks a race whose language that entity doesn't know either -- see
            races.toml's own "language" field and DM_CharacterCreation.py's
            apply_character_creation for how a chosen race's language lands on the player).
        @param target_name The addressed entity, already confirmed present/alive/animate.
        @return (None, None) if at least one language is shared. Otherwise
                (target_language, nonsense_phrase): target_language is the first of target's
                own unshared languages (what a narration prompt names as "the language it
                spoke"), nonsense_phrase is whichever race in races.toml claims that language
                as its own "language" field (see get_race), or None if no race does (ex: a
                scenario-authored language with no matching race entry) -- LLM_Core.py's own
                language-barrier prompt still works without one, just with no style example to
                draw from.
        """
        player_languages = set(self.entities.get(self.player_name, {}).get("languages") or ["common"])
        target_languages = self.entities.get(target_name, {}).get("languages") or ["common"]

        if player_languages.intersection(target_languages):
            return None, None

        target_language = target_languages[0]
        nonsense_phrase = None
        for race in self.rules.get("race", []):
            if race.get("language") == target_language:
                nonsense_phrase = race.get("nonsense_phrase")
                break

        return target_language, nonsense_phrase
