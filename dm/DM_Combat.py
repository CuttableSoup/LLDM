"""!
@file DM_Combat.py
@brief DMCore's side of combat. The logic itself -- damage and its kill consequences, target
    expansion, ability and behavior selection, challenge rating, XP, the lore check -- lives in
    resolution/Combat_Actions.py as functions over DMCore.world; this file supplies the one thing
    those functions can't reach on their own: DMCoreCombatHooks, the CombatHooks they call
    through ctx.hooks for the handful of operations that belong to sibling mixins.
"""

from resolution.Combat_Actions import CombatHooks


class DMCoreCombatHooks(CombatHooks):
    """!@brief CombatHooks over a live DMCore -- each method defers to the mixin that owns it."""

    def __init__(self, core):
        self.core = core

    def is_hostile(self, entity_name, toward_name):
        return self.core.is_hostile(entity_name, toward_name)

    def note_kill(self, killer, victim):
        self.core.note_kill(killer, victim)

    def nudge_combat_hit_attitude(self, target_name, attacker_name, net_damage):
        self.core._nudge_combat_hit_attitude(target_name, attacker_name, net_damage)

    def nudge_attitude_from_event(self, entity_name, toward_name, event_name, magnitude):
        self.core.nudge_attitude_from_event(entity_name, toward_name, event_name, magnitude)

    def move_toward_or_away(self, entity_name, opponent_name, direction):
        return self.core.move_toward_or_away(entity_name, opponent_name, direction)

    def transfer_item(self, from_name, to_name, item_name):
        return self.core.transfer_item(from_name, to_name, item_name)
