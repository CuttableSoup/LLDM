from collections import Counter

from dm.DM_ActionOutcome import CraftEffect, MissingMaterialsOutcome, MissingStationOutcome, NotCraftableOutcome, rolled_outcome_from_roll
from dm.DM_Types import DMCoreProtocol


class CraftingMixin(DMCoreProtocol):
    """!
    @brief Crafting resolution (DMCore mixin -- see DM_Types.py's own module docstring for why
        every mixin inherits DMCoreProtocol). An item's own [entity.craft] block (skill list +
        difficulty + materials, optionally requires_station) is a flat difficulty check --
        never opposed, there's nothing to oppose it with -- resolved the same way an
        [entity.test] check is (DM_Core.py's _resolve_item_test), just keyed by the *result*
        item's own name rather than a live scene target, since a recipe is a pure catalog
        entry that may never have been instanced anywhere (map_to_item already matches over
        every supertype == "object" entity regardless of scene presence -- see NLP_Core.py's
        own item-catalog build). Unlike an ordinary diceless item interaction
        (examine/take/use/...), a craft attempt genuinely rolls dice against a real difficulty,
        so DM_Core.py's _on_turn_detected routes it through the skill/ability side of its
        clause loop (player_actions, dice_penalty) instead of _on_item_interaction_detected.
    """

    def _try_craft_action(self, item_name, dice_penalty):
        """!
        @brief Resolves one craft attempt: gates on the recipe existing, its own
            requires_station (if any) being present in the scene, and every required material
            being in the player's own inventory -- each gate short-circuits with its own
            no-roll "reason", mirroring _resolve_roll's own "out_of_range" no-roll precedent
            (DM_Core.py), before any dice are rolled. Once past every gate, rolls
            resolve_action against the recipe's own difficulty (using the player's best-rated
            skill among the recipe's own skill list -- select_ability_skill, DM_Combat.py,
            already picks a multi-candidate skill this exact way for a multi-skill ability like
            cleave), consumes every material unconditionally (success or failure alike -- a
            botched attempt still spends the materials), and on success places the crafted
            item into the player's own inventory.
        @param item_name The name of the item to craft (already resolved by map_to_item).
        @param dice_penalty Forwarded to resolve_action -- a craft attempt shares the turn's
            own multi-action penalty pool exactly like an item test's own roll already does.
        @return A NotCraftableOutcome/MissingStationOutcome/MissingMaterialsOutcome if a gate
            failed before any roll, else a RolledOutcome (with a CraftEffect on success).
        """
        craft = self.entities.get(item_name, {}).get("craft")
        if not craft:
            return NotCraftableOutcome(self.player_name, item_name)

        station = craft.get("requires_station")
        if station and not self._station_present(station):
            return MissingStationOutcome(self.player_name, item_name, station)

        materials = craft.get("materials", [])
        if not self._has_materials(self.player_name, materials):
            return MissingMaterialsOutcome(self.player_name, item_name, materials)

        skill_name = self.select_ability_skill(self.player_name, {"skill": craft.get("skill", [])})
        roll = self.resolve_action(
            self.player_name, skill_name, craft.get("difficulty", 0), dice_penalty=dice_penalty,
        )

        self._consume_materials(self.player_name, materials)

        effects = []
        if roll["success"]:
            self.place_new_item(self.player_name, item_name)
            effects.append(CraftEffect(item_name=item_name))

        return rolled_outcome_from_roll(roll, effects=effects)

    def _station_present(self, station_tag):
        """!
        @brief Whether any live scene entity's own provides_station names station_tag --
            the first "is a tagged entity present in the scene right now" check anywhere in
            this codebase (every existing requires_condition/blocks_if_condition gate instead
            checks a condition on the acting/target entity's own state, never a third party's
            presence). Scoped to this one narrow field rather than folding into
            entity_matches_requirements, since nothing else needs a scene-presence query yet.
        @param station_tag The station name a recipe's own requires_station names (ex: "forge").
        @return True if a present entity's own provides_station matches.
        """
        return any(
            self.entities.get(name, {}).get("provides_station") == station_tag
            for name in self.scenario_entities
        )

    def _has_materials(self, entity_name, materials):
        """!
        @brief Whether entity_name's own inventory currently holds enough of every material a
            recipe names. Duplicates represent quantity everywhere else in this codebase (see
            transfer_item's own docstring), so this counts occurrences rather than checking
            presence alone.
        @param entity_name The entity whose inventory is checked (always the player today).
        @param materials A recipe's own materials list ({item, quantity} tables).
        @return True if every named item's required quantity is met.
        """
        counts = Counter(self.entities.get(entity_name, {}).get("inventory", []))
        return all(counts[material["item"]] >= material.get("quantity", 1) for material in materials)

    def _consume_materials(self, entity_name, materials):
        """!
        @brief Removes every recipe material from entity_name's own inventory, one occurrence
            per unit of quantity -- called only once _has_materials has already confirmed
            enough of each is present.
        @param entity_name The entity whose inventory materials are removed from.
        @param materials A recipe's own materials list ({item, quantity} tables).
        """
        inventory = self.entities.get(entity_name, {}).get("inventory", [])
        for material in materials:
            for _ in range(material.get("quantity", 1)):
                inventory.remove(material["item"])
