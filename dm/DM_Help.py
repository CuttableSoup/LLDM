from dm.DM_Types import DMCoreProtocol


class HelpMixin(DMCoreProtocol):
    """!
    @brief Out-of-character help/guidance (DMCore mixin -- only ever composed into DMCore,
        never instantiated on its own; relies on self.entities/self.player_name/
        self.scenario_entities/self.rooms/self.locations/self.current_location_key/
        self.event_bus, set up by DMCore.__init__, plus RulesMixin's
        _current_room/_current_scene_name/_current_scene_description/
        _describe_scenario_characters and CombatMixin's resolve_ability. Inherits
        DMCoreProtocol purely so type checkers can resolve these shared attributes/cross-mixin
        methods -- see DM_Types.py.

        A fourth diceless channel, alongside skill/dice actions, item/scene intents, and
        free-form dialogue (see DM_Dialogue.py) -- but unlike all three of those, this one
        isn't in-fiction at all. "ADaM" (see NLP_Core.py's ADAM_NAME_PATTERN) is a reserved
        persona name, not a scene entity, so there's no addressee to resolve and no way for
        this to fail to find someone -- it always resolves, gathering a fresh snapshot of the
        player's own mechanical state and the current scene every time it's invoked. LLMCore's
        own side (generate_adam_response/_build_adam_system_message/_queue_adam_response,
        LLM_Core.py) is what actually turns this payload into a reply -- see its own module
        notes for why that reply is deliberately excluded from context_window entirely,
        instead of merely presence-untagged the way dialogue/narration entries are.

        No longer strictly side-effect-free: when NLP_Core.py's own REMOVAL_KEYWORDS/
        CREATURE_KEYWORDS/EDIT_KEYWORDS gates flagged the input as a plausible removal/creature-
        conjuring/edit request, _on_help_detected also attempts the matching one (see
        _on_help_detected's own docstring) via ImprovisationMixin's own
        _attempt_entity_removal/_attempt_creature_conjuring/_attempt_entity_edit
        (DM_Improvisation.py) *before* gathering the informational payload below -- the one
        exception to "never mutates state" above, since any of the three can change HP/
        equipment/inventory/scenario_entities/an entity's own description or conditions.
        Deliberately still gated behind explicitly addressing ADaM by name, never an automatic
        fallback the way ad hoc item *creation* is -- see DM_Improvisation.py's own module
        docstring for why these have such different risk profiles/triggers than plain item
        creation.

        A fifth diceless channel, _on_scene_query_detected, lives alongside ADaM here too --
        a free-standing "what do I see"/"who is here" question (NLP_Core.py's own
        SCENE_QUERY_KEYWORDS), answered from the same kind of live ground-truth snapshot as
        above, but *without* requiring "adam" to be said at all, and never running the
        removal/creature/edit gates -- purely read-only. LLMCore answers it in the ordinary
        in-fiction Game Master voice, not ADaM's own persona (generate_scene_query_response/
        _build_scene_query_system_message), and its exchange joins context_window normally,
        unlike ADaM's own deliberately-excluded one.
    """

    def _describe_player_skills(self):
        """!
        @brief Formats the player's own [entity.skills] table for ADaM's own system message --
            "name: XD+Y" per skill, sorted by name, straight off live state (post character
            creation, post any future mid-game skill changes) rather than the static template.
        @return A list of formatted skill strings.
        """
        skills = self.entities.get(self.player_name, {}).get("skills", {})
        return [
            f"{name}: {values.get('dice', 0)}D+{values.get('pips', 0)}"
            for name, values in sorted(skills.items())
        ]

    def _describe_player_abilities(self):
        """!
        @brief Formats the player's own flat "abilities" list for ADaM's own system message,
            resolving each entry (a shared catalog reference or an inline table -- see
            CombatMixin's resolve_ability) to its own name/description, the same way any other
            ability lookup in this codebase already does.
        @return A list of formatted ability strings ("name: description", or just "name" if
                the resolved table has no description).
        """
        described = []
        for entry in self.entities.get(self.player_name, {}).get("abilities", []):
            resolved = self.resolve_ability(entry)
            if not resolved:
                continue
            name = resolved.get("name", "")
            description = resolved.get("description", "")
            described.append(f"{name}: {description}" if description else name)
        return described

    def _describe_ground_items(self):
        """!
        @brief Every item currently lying loose in the current room/location (see
            DM_Inventory.py's _current_ground_items), resolved to a "name: description" line
            the same way _describe_player_abilities already resolves the player's own
            abilities -- skips anything still "hidden" (mirrors _describe_scenario_characters'
            own is_hidden check) so a dropped item under an as-yet-unpassed [entity.notice]
            check never gets spoiled here either, even though nothing shipped today actually
            authors one on a loose item.
        @return A list of formatted item strings ("name: description", or just the bare name
                if the resolved entity has no description).
        """
        described = []
        for item_name in self._current_ground_items():
            if self.is_hidden(item_name):
                continue
            description = self.entities.get(item_name, {}).get("description", "")
            described.append(f"{item_name}: {description}" if description else item_name)
        return described

    def _describe_available_exits(self):
        """!
        @brief Every declared way out of here: the current room's own [[location.room.exit]]
            list (a band-gated direction, ex: "forward") if a room is active, plus the current
            location's own [[location.exit]] list (a named destination, ex: "the blacksmith" --
            no direction, reachable from anywhere in the location) if it has any. Reports each
            exit's own "destination" key resolved to that place's friendly "name" (falling back
            to the raw key if it's somehow unresolvable) rather than the bare key itself, so
            ADaM's reply reads as a place name, not internal data.
        @return A list of {"direction", "destination_name"} dicts, room exits (if any) first,
                then location exits ("direction" always None for these) -- [] if neither this
                room nor this location declares any.
        """
        exits = []
        room = self._current_room()
        if room:
            exits.extend(
                {
                    "direction": exit_def.get("direction"),
                    "destination_name": self.rooms.get(exit_def.get("destination"), {}).get(
                        "name", exit_def.get("destination")
                    ),
                }
                for exit_def in room.get("exit", [])
            )
        location = self.locations.get(self.current_location_key, {})
        exits.extend(
            {
                "direction": None,
                "destination_name": self.locations.get(exit_def.get("destination"), {}).get(
                    "name", exit_def.get("destination")
                ),
            }
            for exit_def in location.get("exit", [])
        )
        return exits

    def _on_help_detected(self, data):
        """!
        @brief Event handler for "help_detected" (NLPCore's own ADAM_NAME_PATTERN match) --
            gathers a fresh snapshot of the player's own mechanical state and the current
            scene and publishes it as "help_resolved" for LLMCore to narrate as ADaM. Rolls no
            dice; mutates state only in the cases NLP_Core.py's own REMOVAL_KEYWORDS/
            CREATURE_KEYWORDS/EDIT_KEYWORDS gates flagged as a plausible request (see this
            class's own module docstring) -- each of ImprovisationMixin's own
            _attempt_entity_removal/_attempt_creature_conjuring/_attempt_entity_edit
            (DM_Improvisation.py) runs independently (a single ADaM message could plausibly
            trigger more than one, though in practice it's almost always at most one) and its
            outcome is folded into the payload as "removed"/"created_creature"/"edited", so
            ADaM's own narration can mention what happened. _publish_party_status() is only
            called when at least one of the three actually went through (any of them can change
            HP/equipment/inventory/scenario_entities/an entity's own description or conditions,
            so the GUI's Party tab needs to redraw); the ordinary informational path still never
            calls it, the same reasoning DialogueMixin already documents for why dialogue never
            does either.
        @param data The "help_detected" payload ({"input": processed_text, "removal_candidate",
            "creature_candidate", "edit_candidate": bool}).
        """
        removal_outcome = None
        if data.get("removal_candidate"):
            removal_outcome = self._attempt_entity_removal(data.get("input", ""))

        creature_outcome = None
        if data.get("creature_candidate"):
            creature_outcome = self._attempt_creature_conjuring(data.get("input", ""))

        edit_outcome = None
        if data.get("edit_candidate"):
            edit_outcome = self._attempt_entity_edit(data.get("input", ""))

        player = self.entities.get(self.player_name, {})
        self.event_bus.publish("help_resolved", {
            "input": data.get("input", ""),
            "present_entities": list(self.scenario_entities),
            "skills": self._describe_player_skills(),
            "abilities": self._describe_player_abilities(),
            "equipped": dict(player.get("equipped", {})),
            "inventory": list(player.get("inventory", [])),
            "scene_name": self._current_scene_name(),
            "scene_description": self._current_scene_description(),
            "present": self._describe_scenario_characters(),
            "exits": self._describe_available_exits(),
            "ground_items": self._describe_ground_items(),
            "removed": removal_outcome,
            "created_creature": creature_outcome,
            "edited": edit_outcome,
        })
        if (
            (removal_outcome and removal_outcome.get("removed"))
            or (creature_outcome and creature_outcome.get("created_creature"))
            or (edit_outcome and edit_outcome.get("edited"))
        ):
            self._publish_party_status()

    def _on_scene_query_detected(self, data):
        """!
        @brief Event handler for "scene_query_detected" (NLP_Core.py's own
            SCENE_QUERY_KEYWORDS match) -- a free-standing, in-fiction "what do I see"/"who is
            here" question, answered from the exact same kind of live ground-truth snapshot
            ADaM's own _on_help_detected already gathers (present roster, scene description,
            exits, ground items). Deliberately its own handler rather than reusing
            _on_help_detected: this channel is strictly read-only, so it never runs the
            REMOVAL_KEYWORDS/CREATURE_KEYWORDS/EDIT_KEYWORDS gates ADaM's own higher-risk
            mutation paths do, and it's meant to be narrated in the ordinary in-fiction Game
            Master voice, not ADaM's own out-of-character persona -- see LLM_Core.py's
            generate_scene_query_response/_build_scene_query_system_message. Publishes no
            "help_resolved" fields it doesn't need (skills/abilities/equipped/inventory are
            the player's own mechanical state, irrelevant to "what does the room look like").
        @param data The "scene_query_detected" payload ({"input": processed_text}).
        """
        self.event_bus.publish("scene_query_resolved", {
            "input": data.get("input", ""),
            "present_entities": list(self.scenario_entities),
            "scene_name": self._current_scene_name(),
            "scene_description": self._current_scene_description(),
            "present": self._describe_scenario_characters(),
            "exits": self._describe_available_exits(),
            "ground_items": self._describe_ground_items(),
        })
