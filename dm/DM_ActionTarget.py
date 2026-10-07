"""!
@file DM_ActionTarget.py
@brief DMCore's side of action target resolution. The logic lives in resolution/Action_Target.py
    as a pure function over an ActionTargetScene; this supplies the one thing it can't reach on
    its own: the scene, read off a live DMCore.
"""

import resolution.Combat_Resolution as Combat_Resolution
from resolution.Action_Target import ActionTargetScene


class DMCoreActionTargetScene(ActionTargetScene):
    """!@brief ActionTargetScene over a live DMCore -- read-only; each query defers to what owns it."""

    def __init__(self, core):
        self.core = core

    @property
    def entities(self):
        return self.core.entities

    @property
    def scenario_entities(self):
        return self.core.scenario_entities

    @property
    def player_name(self):
        return self.core.player_name

    @property
    def current_target(self):
        return self.core.current_target

    @property
    def partner_key(self):
        return (self.core.conversation_partner or {}).get("key")

    def hp(self, key):
        return Combat_Resolution.get_current_hp(self.core.world, key)

    def hp_fraction(self, key):
        return Combat_Resolution.get_comparable_value(self.core.world, key, "hp_per_remain")

    def is_hostile(self, key):
        return self.core.is_hostile(key, self.core.player_name)

    def is_party_member(self, key):
        return self.core._is_party_member(key)

    def is_hidden(self, key):
        return self.core.is_hidden(key)
