"""!
@file DM_Enforcement.py
@brief EnforcementMixin -- DMCore's side of law enforcement (see docs/law.md "Enforcement").

    The confrontation flow, the polity records and the pending reports live in
    resolution/Law_Enforcement.py's LawEnforcement, which reaches the game only through its
    LawWorld port. This file supplies that port (DMCoreLawWorld), builds the LawEnforcement and
    wires the events it listens to, and keeps legal_records/pending_reports/pending_arrest
    readable as DMCore attributes (they delegate -- LawEnforcement owns them).
"""

import resolution.Law_Resolution as Law_Resolution
from dm.DM_Types import DMCoreProtocol
from resolution.Law_Enforcement import LawEnforcement, LawWorld
import resolution.Combat_Resolution as Combat_Resolution


class DMCoreLawWorld(LawWorld):
    """!@brief LawEnforcement's view of a live DMCore -- every method defers to what DMCore (or
        one of its mixins) already does. Attributes are read live, never copied."""

    def __init__(self, core):
        self.core = core

    @property
    def entities(self):
        return self.core.entities

    @property
    def rules(self):
        return self.core.rules

    @property
    def scenario_entities(self):
        return self.core.scenario_entities

    @property
    def player_name(self):
        return self.core.player_name

    @property
    def current_location_key(self):
        return self.core.current_location_key

    @property
    def locations(self):
        return self.core.locations

    @property
    def current_block(self):
        return self.core.current_block

    def current_polity(self):
        return self.core.current_polity()

    def sees_through(self, witness, subject):
        return self.core._sees_through(witness, subject)

    def is_hostile(self, entity_name, toward_name):
        return self.core.is_hostile(entity_name, toward_name)

    def is_party_member(self, entity_name):
        return self.core._is_party_member(entity_name)

    def resolve_action(self, entity_name, skill_name, difficulty=0):
        return Combat_Resolution.resolve_action(self.core.world, entity_name, skill_name, difficulty)

    def nudge_attitude(self, entity_name, toward_name, event_name, magnitude):
        self.core.nudge_attitude_from_event(entity_name, toward_name, event_name, magnitude)

    def transfer_currency(self, from_name, to_name, amount):
        self.core.transfer_currency(from_name, to_name, amount)

    def format_currency(self, amount):
        return self.core.format_currency(amount)

    def enter_location(self, location_key):
        self.core._enter_location(location_key)

    def advance_blocks(self, blocks):
        self.core.advance_blocks(blocks)

    def hours_for_blocks(self, blocks):
        state = self.core.get_time_state()
        return round(blocks * state["hours_per_day"] / state["blocks_per_day"])

    def publish(self, event, payload):
        self.core.event_bus.publish(event, payload)


class EnforcementMixin(DMCoreProtocol):

    def _init_enforcement_state(self):
        """!
        @brief Builds self.law_enforcement and hooks it to the events it reacts to: the start and
            end of each player input (so a confrontation is announced after the crime is
            narrated), the player's reply, and anything that counts as acting on their turn.
        """
        self.law_enforcement = LawEnforcement(DMCoreLawWorld(self))
        law = self.law_enforcement
        self.event_bus.subscribe("player_input_received", law.on_input_started)
        self.event_bus.subscribe("player_input_handled", law.on_input_handled)
        self.event_bus.subscribe("arrest_answered", law.on_arrest_answered)
        self.event_bus.subscribe("turn_detected", law.note_player_acted)
        self.event_bus.subscribe("item_interaction_detected", law.note_player_acted)

    # LawEnforcement owns this state; these keep it readable (and assignable) as DMCore attributes.

    @property
    def legal_records(self):
        return self.law_enforcement.legal_records

    @legal_records.setter
    def legal_records(self, value):
        self.law_enforcement.legal_records = value

    @property
    def pending_reports(self):
        return self.law_enforcement.pending_reports

    @pending_reports.setter
    def pending_reports(self, value):
        self.law_enforcement.pending_reports = value

    @property
    def pending_arrest(self):
        return self.law_enforcement.pending_arrest

    @pending_arrest.setter
    def pending_arrest(self, value):
        self.law_enforcement.pending_arrest = value

    def _is_enforcer(self, name):
        return Law_Resolution.is_enforcer(self.entities, name)
