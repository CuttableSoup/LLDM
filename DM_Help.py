from DM_Types import DMCoreProtocol


class HelpMixin(DMCoreProtocol):
    """!
    @brief Out-of-character help/guidance (DMCore mixin -- only ever composed into DMCore,
        never instantiated on its own; relies on self.entities/self.player_name/
        self.scenario_entities/self.rooms/self.event_bus, set up by DMCore.__init__, plus
        RulesMixin's _current_room/_current_scene_name/_current_scene_description/
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

    def _describe_available_exits(self):
        """!
        @brief The current room's own declared exits, for a multi-room dungeon -- [] for a
            flat single-room scenario (self.rooms empty), which has no [[room.exit]] tables
            at all. Reports each exit's own "destination" room key resolved to that room's
            friendly "name" (falling back to the raw key if it's somehow unresolvable) rather
            than the bare key itself, so ADaM's reply reads as a place name, not internal data.
        @return A list of {"direction", "destination_name"} dicts, in the room's own
                declaration order.
        """
        if not self.rooms:
            return []
        room = self._current_room() or {}
        return [
            {
                "direction": exit_def.get("direction"),
                "destination_name": self.rooms.get(exit_def.get("destination"), {}).get(
                    "name", exit_def.get("destination")
                ),
            }
            for exit_def in room.get("exit", [])
        ]

    def _on_help_detected(self, data):
        """!
        @brief Event handler for "help_detected" (NLPCore's own ADAM_NAME_PATTERN match) --
            gathers a fresh snapshot of the player's own mechanical state and the current
            scene and publishes it as "help_resolved" for LLMCore to narrate as ADaM. Never
            rolls dice, never mutates state (no _publish_party_status() call, the same
            reasoning DialogueMixin already documents for why dialogue never calls it either).
        @param data The "help_detected" payload ({"input": processed_text}).
        """
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
        })
