"""!
@file DM_NpcGeneration.py
@brief DMCore mixin turning an entity_template (see Rules/Fantasy/scenarios/tavern_random.toml,
    DM_Rules.py's _instance_entities) into a real stat block at the moment it's instanced.
    Pure math/LLM-calling logic lives in NPC_Generation.py -- this mixin is the "glue" that
    resolves a template's own target_cr against live DMCore state and bakes the result onto
    the live entity, the same split DM_CharacterCreation.py is to Character_Creation.py.
"""

from Challenge_Rating import calculate_party_challenge_rating
from DM_Types import DMCoreProtocol
from NPC_Generation import generate_npc_stats, load_npc_keywords, resolve_varied_value

# Fallback target CR for an entity_template whose own "target_cr" field is missing or
# unrecognized -- should never come up with valid authoring (a log_warning fires alongside
# it), just keeps generation from crashing on bad data.
DEFAULT_GENERATED_CR = 20

# Mirrors DM_Rules.py's own PLAYER_PLACEHOLDER value (not imported directly -- these are
# sibling mixin files, and neither needs the other's rules-loading internals, just the same
# reserved token). An entity_template authors an [[entity_template.attitudes.name]] override
# toward this literal token when it wants to single out "the player" specifically, without
# knowing ahead of time what a freshly-created or renamed character will actually be called
# (see DM_CharacterCreation.py) -- resolved to self.player_name the moment the template is
# baked into a live instance (see _resolve_generated_attitudes).
PLAYER_ATTITUDE_TOKEN = "player"


class NpcGenerationMixin(DMCoreProtocol):
    """!
    @brief Only ever called from RulesMixin's _instance_entities, right after an instance
        resolved from self.entity_templates (not self.entities) is stored into
        self.entities -- relies on self.entities/self.player_name/self.event_bus, set up by
        DMCore.__init__, plus get_challenge_rating (CombatMixin). Inherits DMCoreProtocol
        purely so type checkers can resolve these shared attributes/cross-mixin methods --
        see DM_Types.py.
    """

    def _resolve_npc_target_cr(self, target_cr_field, party_pool, instance_names_so_far):
        """!
        @brief Resolves a generate=true template's own "target_cr" field to an actual number.
        @param target_cr_field A number, or the literal strings "player"/"party".
        @param party_pool Entities already known to be part of the party before this
            _instance_entities call started (self.persistent_entities for a room-level call,
            [] for the top-level scenario call). self.scenario_entities itself isn't used
            here on purpose -- it isn't finalized until the whole _instance_entities call it's
            being built by actually returns, so it's either empty (fresh boot) or stale (a
            load_game re-run) for exactly the entries this method is resolving mid-loop.
        @param instance_names_so_far Instance names already created earlier in this same
            _instance_entities loop -- combined with party_pool to build a *live* party
            roster without relying on self.scenario_entities.
        @return The resolved target CR (a number).
        """
        if isinstance(target_cr_field, (int, float)):
            return target_cr_field
        if target_cr_field == "player":
            return self.get_challenge_rating(self.player_name)
        if target_cr_field == "party":
            pool = list(party_pool) + [
                name for name in instance_names_so_far
                if name == self.player_name or self.entities.get(name, {}).get("is_party")
            ]
            return calculate_party_challenge_rating(self.get_challenge_rating(name) for name in pool)
        self.event_bus.publish(
            "log_warning",
            f"generate=true template has an unrecognized target_cr {target_cr_field!r}; "
            f"falling back to {DEFAULT_GENERATED_CR}.",
        )
        return DEFAULT_GENERATED_CR

    def _apply_npc_generation(self, instance_name, party_pool, instance_names_so_far, skip_llm_generation):
        """!
        @brief Mutates self.entities[instance_name] in place: resolves its own target_cr and
            any varied fields (hint/cr_multiplier/currency/qualities/attitudes -- see
            NPC_Generation.py's resolve_varied_value), generates a name/backstory/skills/
            max_hp (or takes the offline fallback path -- see skip_llm_generation), and tags
            it generated = True so DM_Persistence.py's save_game knows to persist all of this
            dynamic state (none of it derives from any static template the way an ordinary
            entity's own fields do). Deliberately does *not* touch abilities/equipped/
            inventory at all -- combat/dialogue capability is still decided separately, by
            whoever authors the entity_template (same as any hand-authored entity; see
            CLAUDE.md's "NPC generation").
        @param instance_name The entity's own key in self.entities (already stored there by
            the time this runs -- see _instance_entities).
        @param party_pool See _resolve_npc_target_cr.
        @param instance_names_so_far See _resolve_npc_target_cr.
        @param skip_llm_generation True while re-instancing during a save-game load -- skips
            the network call entirely (NPC_Generation.generate_npc_stats' own offline
            fallback path), since whatever it would produce is about to be overwritten by the
            saved values anyway (see DM_Persistence.py's load_game).
        """
        entity = self.entities[instance_name]
        target_cr = self._resolve_npc_target_cr(
            entity.get("target_cr"), party_pool, instance_names_so_far,
        )
        npc_keywords = load_npc_keywords()

        # hint/cr_multiplier/qualities all feed generation itself, so they're resolved first --
        # qualities specifically has to be concrete (gender/race/age already picked, not a
        # {min, max}/weighted-choice table) *before* generate_npc_stats runs, so the LLM's own
        # invented name/backstory can actually agree with them (ex: a resolved gender = "male"
        # shouldn't come back paired with a name the model would only ever give a woman) --
        # currency/attitudes have no bearing on the LLM prompt and can resolve either side of it.
        hint = resolve_varied_value(entity.get("hint"))
        cr_multiplier = resolve_varied_value(entity.get("cr_multiplier", 1.0))
        self._resolve_generated_qualities(entity)

        if not skip_llm_generation:
            self.event_bus.publish("log_info", f"Generating NPC for '{instance_name}'...")

        result = generate_npc_stats(
            npc_keywords, target_cr,
            hint=hint,
            qualities=entity.get("qualities"),
            variance=entity.get("variance", 0.15),
            cr_multiplier=cr_multiplier,
            skip_llm_generation=skip_llm_generation,
        )

        entity["name"] = result["name"]
        entity["description"] = result["description"]
        entity["skills"] = result["skills"]
        entity["max_hp"] = result["max_hp"]
        entity["generated"] = True

        if "currency" in entity:
            entity["currency"] = resolve_varied_value(entity["currency"])
        self._resolve_generated_attitudes(entity)

        self.event_bus.publish("log_info", f"Generated NPC '{instance_name}': {result['name']}.")

    def _resolve_generated_qualities(self, entity):
        """!
        @brief Resolves every [entity_template.qualities] field that may be varied (ex:
            tavern_random.toml's own generated_stranger, its weighted "race"/"gender", ranged
            "age") down to one concrete value each -- in place, so a fixed field (ex: the same
            file's generated_innkeeper's plain race = "dwarf") passes through
            resolve_varied_value unchanged.
        @param entity The live instance dict (already carrying whatever [entity.qualities]
            the entity_template declared, via _instance_entities' own deepcopy).
        """
        qualities = entity.get("qualities")
        if not qualities:
            return
        for key, value in list(qualities.items()):
            qualities[key] = resolve_varied_value(value)

    def _resolve_generated_attitudes(self, entity):
        """!
        @brief Resolves [entity.attitudes] down to concrete numbers, element by element (not
            the six-axis array as a single varied field -- resolve_varied_value is applied to
            each axis individually, so a template can mix fixed and varied axes freely, ex:
            tavern_random.toml's generated_stranger keeping trust/confidence at a flat 0 while
            disposition/intimacy vary). Covers "default" plus every "name"/"supertype"
            override entry the same way. Also substitutes the reserved PLAYER_ATTITUDE_TOKEN
            key in any "name" override for self.player_name -- an entity_template can't know
            ahead of time what a freshly-created/renamed character is actually called (see
            DM_CharacterCreation.py), so it authors toward this placeholder instead, the same
            way a scenario's own "entities" list uses DM_Rules.py's PLAYER_PLACEHOLDER.
        @param entity The live instance dict.
        """
        attitudes = entity.get("attitudes")
        if not attitudes:
            return

        if "default" in attitudes:
            attitudes["default"] = [resolve_varied_value(axis) for axis in attitudes["default"]]

        for override_kind in ("name", "supertype"):
            overrides = attitudes.get(override_kind)
            if not overrides:
                continue
            attitudes[override_kind] = [
                {key: [resolve_varied_value(axis) for axis in axes] for key, axes in override.items()}
                for override in overrides
            ]

        for override in attitudes.get("name", []):
            if PLAYER_ATTITUDE_TOKEN in override:
                override[self.player_name] = override.pop(PLAYER_ATTITUDE_TOKEN)
