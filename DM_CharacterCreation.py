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
            point-buy allocation, and -- if character carries a non-blank "name" different
            from self.player_name -- renames the player entity itself, re-keying self.entities
            and updating self.player_name to match. Called from DMCore.__init__ right after
            _resolve_player_name() resolves self.player_name, and before
            load_scenario_definition()/load_scenario() run -- so both the skill override and
            any rename are what _instance_entities later deep-copies into the live scenario
            instance (via PLAYER_PLACEHOLDER, DM_Rules.py), not the original TOML-authored
            template.

            A no-op if character is falsy (None) -- what every existing boot path/test still
            exercises today, keeping the player template's own hand-authored skills exactly as
            load_rules left them. A non-empty "allocation" is validated the same as always
            (rejected with a logged error, not a raised exception -- same "malformed data
            degrades quietly" convention load_rules' own per-file try/except follows -- so a
            bad character-creation result can never leave the player with a half-applied or
            illegally-large skill set); an absent/empty "allocation" skips the skill/race
            override step entirely and leaves the template's own skills untouched, which is
            what lets LLDM.py's CLI quick-boot path pass a bare `{"name": ...}` (a rename with
            no point-buy sheet behind it at all) through this exact same method rather than
            needing its own separate rename-only code path.
        @param character {"race": race_name, "allocation": {skill_name: dice_int}, "name":
            new_name}, or None. "allocation"/"race" and "name" are independent of each other --
            either can be given without the other. "name" absent/blank/unchanged leaves
            self.player_name exactly as _resolve_player_name found it.
        """
        if not character:
            return

        player = self.entities.get(self.player_name)
        if player is None:
            return

        allocation = character.get("allocation") or {}
        race_name = character.get("race")
        if allocation:
            race = get_race(self.rules.get("race", []), race_name)
            character_creation = self.rules.get("character_creation", {})

            ok, reason = validate_allocation(self.skills, race, character_creation, allocation)
            if not ok:
                self.event_bus.publish(
                    "log_error", f"Character creation rejected ({race_name}): {reason}"
                )
                return

            player["skills"] = build_character_skills(self.skills, race, allocation)
            if race_name and "qualities" in player:
                player["qualities"]["race"] = race_name

        new_name = (character.get("name") or "").strip()
        if new_name and new_name != self.player_name:
            if new_name in self.entities:
                # Refuses to clobber another already-loaded template/entity under the same
                # key (ex: naming a character "wolf") -- the skill/race override above still
                # applies, only the identity change is rejected, so a bad name can't silently
                # corrupt unrelated game data.
                self.event_bus.publish(
                    "log_error",
                    f"Character creation rename rejected: '{new_name}' already names "
                    "another entity.",
                )
            else:
                del self.entities[self.player_name]
                player["name"] = new_name
                self.entities[new_name] = player
                self.player_name = new_name

        suffix = f" is now a {race_name}." if allocation else "."
        self.event_bus.publish(
            "log_info", f"Character creation applied: {self.player_name}{suffix}"
        )
