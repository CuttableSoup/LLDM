from dm.DM_Types import DMCoreProtocol
from resolution.Character_Creation import (
    build_character_skills, get_race, spend_exp_on_skills, validate_allocation,
)


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
            point-buy allocation -- also appending the chosen race's own "language" (races.toml)
            onto the player's own "languages" list if it isn't already there (ex: an elf adds
            "elvish" onto the template's default ["common"]; a human re-adding "common" is a
            no-op), which is what DM_Dialogue.py's language-barrier check reads to decide
            whether the player shares a tongue with whoever they're addressing -- and -- if
            character carries a non-blank "name" different
            from self.player_name -- renames the player entity itself, re-keying self.entities,
            updating self.player_name to match, and rewriting any other entity's own
            [[entity.attitudes.name]] override still keyed to the old name (_rekey_attitude_
            overrides, below) so a hand-authored disposition toward the player's original
            template name (ex: characters.toml's own "anne" override keyed to "gladstone")
            keeps applying to whoever they were actually renamed to. Called from
            DMCore.__init__ right after load_scenario_definition() loads the scenario's own
            TOML data (so the rename's own collision check below sees scenario-local entity
            names too, not just the shared Rules/<setting>/*.toml catalog), but before
            load_scenario() actually instances anything -- so both the skill override and
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

            A non-empty "pip_spend" (see Character_Creation.py's spend_exp_on_skills) then
            trains skills further, on top of whatever player["skills"] already is at that point
            -- the just-built point-buy result if "allocation" ran, or the template's own
            hand-authored skills otherwise -- spending from the player template's own "exp"
            field (ex: characters.toml's gladstone, exp = 100). Replayed fresh here, never
            trusting a client-submitted final {dice, pips}, same "recompute, don't trust"
            precedent "allocation" already sets; a rejected spend (an unknown skill, or running
            out of XP partway through) is logged and left entirely unapplied -- unlike a
            rejected "allocation", this doesn't abort the rest of the method, since training and
            renaming are unrelated.
        @param character {"race": race_name, "allocation": {skill_name: dice_int}, "pip_spend":
            [skill_name, ...], "name": new_name}, or None. "allocation"/"race", "pip_spend", and
            "name" are independent of each other -- any can be given without the others. "name"
            absent/blank/unchanged leaves self.player_name exactly as _resolve_player_name found
            it.
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

            race_language = (race or {}).get("language")
            if race_language:
                languages = list(player.get("languages") or ["common"])
                if race_language not in languages:
                    languages.append(race_language)
                player["languages"] = languages

        pip_spend = character.get("pip_spend") or []
        if pip_spend:
            new_skills, remaining_exp, reason = spend_exp_on_skills(
                player.get("skills", {}), player.get("exp", 0), pip_spend,
            )
            if reason:
                self.event_bus.publish("log_error", f"Character creation XP spend rejected: {reason}")
            else:
                player["skills"] = new_skills
                player["exp"] = remaining_exp

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
                old_name = self.player_name
                del self.entities[self.player_name]
                player["name"] = new_name
                self.entities[new_name] = player
                self.player_name = new_name
                self._rekey_attitude_overrides(old_name, new_name)

        suffix = f" is now a {race_name}." if allocation else "."
        self.event_bus.publish(
            "log_info", f"Character creation applied: {self.player_name}{suffix}"
        )

    def _rekey_attitude_overrides(self, old_name, new_name):
        """!
        @brief Rewrites every other entity's own [[entity.attitudes.name]] override still keyed
            to old_name so it keeps applying after a character-creation rename -- get_attitude's
            own name-keyed lookup (DM_Social.py) matches by literal string against toward_name,
            so a hand-authored override written against the player's original template name
            (ex: characters.toml's own "anne" -- attitudes.name.gladstone = [100, 100, 100])
            would otherwise silently stop resolving to whoever the player actually renamed
            themselves to, reverting to that entity's own "default"/supertype tier instead with
            no error or narration hinting why.
        @param old_name The player's own name before the rename.
        @param new_name The player's own name after the rename.
        """
        for entity in self.entities.values():
            overrides = entity.get("attitudes", {}).get("name")
            if not overrides:
                continue
            for override in overrides:
                if old_name in override:
                    override[new_name] = override.pop(old_name)
