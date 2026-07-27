from DM_Types import DMCoreProtocol
from Character_Creation import build_character_skills, get_race, validate_allocation


class CharacterCreationMixin(DMCoreProtocol):
    """!
    @brief Bakes a finished character-creation result into the player entity (DMCore mixin --
        only ever composed into DMCore, never instantiated on its own; relies on
        self.entities/self.skills/self.rules/self.player_name/self.event_bus, set up by
        DMCore.__init__). The point-buy math itself lives in Character_Creation.py (pure,
        UI-agnostic, importable before any DMCore exists at all -- see its own module
        docstring); this mixin's only job is applying an already-built {race, allocation}
        result onto self.entities[self.player_name], the same "instancing overwrites the
        template slot" convention every other per-instance mutation in this codebase follows
        (see CLAUDE.md's "Scenario instancing").
    """

    def apply_character_creation(self, character):
        """!
        @brief Overwrites the player template's own "skills" (and its "qualities.race" flavor
            field, if it has a qualities table) with a freshly-created character's race and
            point-buy allocation. Called from DMCore.__init__ right after
            _resolve_player_name() resolves self.player_name, and before
            load_scenario_definition()/load_scenario() run -- so the override is what
            _instance_entities later deep-copies into the live scenario instance, not the
            original TOML-authored skills.

            A no-op if character is falsy (None) -- what every existing boot path/test still
            exercises today, keeping the player template's own hand-authored skills exactly as
            load_rules left them. Also a no-op (with a logged error, not a raised exception --
            same "malformed data degrades quietly" convention load_rules' own per-file
            try/except follows) if the allocation itself fails validate_allocation, so a bad
            character-creation result can never leave the player with a half-applied or
            illegally-large skill set.
        @param character {"race": race_name, "allocation": {skill_name: dice_int}}, or None.
        """
        if not character:
            return

        race_name = character.get("race")
        allocation = character.get("allocation", {})
        race = get_race(self.rules.get("race", []), race_name)
        character_creation = self.rules.get("character_creation", {})

        ok, reason = validate_allocation(self.skills, race, character_creation, allocation)
        if not ok:
            self.event_bus.publish(
                "log_error", f"Character creation rejected ({race_name}): {reason}"
            )
            return

        player = self.entities.get(self.player_name)
        if player is None:
            return

        player["skills"] = build_character_skills(self.skills, race, allocation)
        if race_name and "qualities" in player:
            player["qualities"]["race"] = race_name
        self.event_bus.publish(
            "log_info", f"Character creation applied: {self.player_name} is now a {race_name}."
        )
