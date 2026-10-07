"""!
@file World_Context.py
@brief WorldContext -- the live state the pure resolution functions (Combat_Resolution.py,
    Combat_Actions.py) read and write: the entities dict, the loaded rules, the skills table, the
    event bus, and who is in the scene. DMCore holds exactly one (DMCore.world) and exposes
    entities/rules/skills as read-only properties over it, so nothing can rebind them out from
    under a function that was handed the context; scenario_entities, player_name and
    universal_abilities are replaced wholesale during play, so DMCore's properties for those can
    be assigned and the context always reads the current value. Every Combat_Resolution function
    takes it as its first argument.

    A test or a caller with only part of the picture builds one directly --
    WorldContext(entities) is enough for a function that only reads entities. The fields it
    wasn't given default to empty (rules, skills, scenario_entities, universal_abilities) or None
    (event_bus, player_name, hooks), so a function that needs one it wasn't given fails loudly at
    the first use rather than reading stale state.
"""


# The reserved scenario-entity name that resolves to the active player character (DM_Rules.py's
# instancing, Data_Validation.py's reference checks).
PLAYER_PLACEHOLDER = "player"


class WorldContext:
    """!
    @brief A bundle of references, never a copy: mutating context.entities mutates the same dict
        DMCore holds. hooks is the CombatHooks (Combat_Actions.py) that reaches the few sibling
        mixins the combat graph can't -- None where nothing needs them.
    """

    __slots__ = (
        "entities", "rules", "skills", "event_bus", "scenario_entities", "player_name",
        "universal_abilities", "hooks",
    )

    def __init__(
        self, entities=None, rules=None, skills=None, event_bus=None, scenario_entities=None, player_name=None,
        universal_abilities=None, hooks=None,
    ):
        self.entities = {} if entities is None else entities
        self.rules = {} if rules is None else rules
        self.skills = {} if skills is None else skills
        self.event_bus = event_bus
        self.scenario_entities = [] if scenario_entities is None else scenario_entities
        self.player_name = player_name
        self.universal_abilities = {} if universal_abilities is None else universal_abilities
        self.hooks = hooks

    def get_equip_slots(self, entity_name):
        """!
        @brief Resolves the valid [entity.equipped] slot names for entity_name, from
            rules.toml's own [[equip_slot]] table: a "subtype"-specific entry for this
            entity's own supertype beats a supertype-only entry (no "subtype" key at all),
            same override precedence as get_attitude's name/supertype/default lookup.
        @param entity_name The name of the entity (template or live instance) to look up.
        @return The list of valid slot names, or [] if no [[equip_slot]] entry matches this
                entity's own supertype/subtype at all.
        """
        entity = self.entities.get(entity_name, {})
        supertype = entity.get("supertype")
        subtype = entity.get("subtype")

        supertype_only_slots = None
        for rule in self.rules.get("equip_slot", []):
            if rule.get("supertype") != supertype:
                continue
            if "subtype" in rule:
                if rule.get("subtype") == subtype:
                    return list(rule.get("slots", []))
            elif supertype_only_slots is None:
                supertype_only_slots = list(rule.get("slots", []))

        return supertype_only_slots if supertype_only_slots is not None else []
