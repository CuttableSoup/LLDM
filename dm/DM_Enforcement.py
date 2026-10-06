"""!
@file DM_Enforcement.py
@brief EnforcementMixin -- what a guard does about a crime (see docs/law.md "Enforcement").

    DM_Law.py records crimes; this acts on them. An enforcer (tag law_enforcer) who witnesses a
    crime confronts the player at once, whatever the bounty. One who meets a wanted player
    later has to recognize them first -- the same disguise-then-acclaim check witnesses use --
    and only confronts a bounty of at least the polity's arrest_at; at kill_on_sight_at it
    attacks instead. Everything here is decided by rolls and records, never by the narrator.

    A confrontation is DMCore.pending_arrest. It is announced once the player's input has
    finished resolving ("player_input_handled"), so the guard steps in after the crime is
    narrated, not before. NLPCore reads the next input as the reply ("arrest_answered"): pay,
    surrender, bribe, bluff or resist. Anything else is played as usual -- but attacking anyone
    or leaving counts as resisting, and carrying on with something else twice does too.
"""

from dm.DM_Types import DMCoreProtocol
import resolution.Law_Resolution as Law_Resolution
from resolution.Inventory_Resolution import _settle, parse_currency_amount

# An enforcer tagged this turns every bribe down without a roll.
INCORRUPTIBLE_TAG = "incorruptible"

# The replies a confrontation offers, in the order the notice lists them.
ARREST_CHOICES = ("pay", "surrender", "bribe", "bluff", "resist")

# How many times the player may carry on with something else before that counts as resisting.
STALL_LIMIT = 2

class EnforcementMixin(DMCoreProtocol):

    def _init_enforcement_state(self):
        """!
        @brief pending_arrest: the open confrontation, or None -- {"enforcer", "polity",
            "identity", "witnessed", "location", "tried", "strikes", "announced"}. Saved.
            _arrest_announcements: arrest events waiting for the end of the current input.
            _arrest_input: what the current input did that a confrontation cares about.
        """
        self.pending_arrest = None
        self._arrest_announcements = []
        self._handling_input = False
        self._arrest_input = {"answer": None, "acted": False, "assaulted": False}
        self.event_bus.subscribe("player_input_received", self._on_enforcement_input_started)
        self.event_bus.subscribe("player_input_handled", self._on_player_input_handled)
        self.event_bus.subscribe("arrest_answered", self._on_arrest_answered)
        self.event_bus.subscribe("turn_detected", self._note_player_acted)
        self.event_bus.subscribe("item_interaction_detected", self._note_player_acted)

    # -- Who enforces, and whom they know ------------------------------------------------

    def _present_enforcers(self):
        """!@brief Living enforcers in the scene who aren't already fighting the player."""
        return [
            name for name in self.scenario_entities
            if self._is_enforcer(name) and self.get_current_hp(name) > 0
            and not self._is_party_member(name) and not self.is_hostile(name, self.player_name)
        ]

    def _polity_enforcement(self, polity_name):
        """!@return (arrest_at, kill_on_sight_at) for the polity -- None for either means off."""
        polity = self._find_polity(polity_name) or {}
        return polity.get("arrest_at"), polity.get("kill_on_sight_at")

    def _record_bounty(self, polity, identity):
        return (self.legal_records.get(polity, {}).get(identity) or {}).get("bounty", 0) or 0

    def _presented_identities(self):
        """!
        @return [(identity, needs_see_through)] -- who the player shows the world: a disguise's
                own identity (seen by anyone looking), then the player beneath it, who has to be
                seen through first.
        """
        disguise = self.entities.get(self.player_name, {}).get("disguise")
        if disguise:
            return [(disguise["identity"], False), (self.player_name, True)]
        return [(self.player_name, False)]

    def _enforcer_recognizes(self, enforcer, identity, polity, needs_see_through):
        """!
        @brief Whether enforcer knows the player as identity -- seen through any disguise first
            (DM_Law.py's _sees_through) when identity is the face beneath it, then either having
            watched that identity commit a crime, or the acclaim roll a banned presence gets
            ([law].recognition_skill against the [[law.recognition]] band). Checked once per
            enforcer per identity per disguise.
        """
        disguise = self.entities.get(self.player_name, {}).get("disguise") or {}
        key = f"{identity}|{disguise.get('identity', '')}"
        checks = self.entities[enforcer].setdefault("enforcement_checks", {})
        if key in checks:
            return checks[key]
        if needs_see_through and not self._sees_through(enforcer, self.player_name):
            recognized = False
        elif any(seen.get("offender") == identity for seen in self.entities[enforcer].get("known_crimes", [])):
            recognized = True
        else:
            settings = self._law_settings()
            subject = self.entities.get(identity, {}) if identity in self.entities else {}
            record = self.legal_records.get(polity, {}).get(identity)
            _, magnitude = Law_Resolution.effective_acclaim(subject, record)
            difficulty = Law_Resolution.recognition_difficulty(
                magnitude, settings.get("recognition", []), self.rules.get("difficulty_tier", []),
            )
            if difficulty is None:
                recognized = False
            elif difficulty == 0:
                recognized = True
            else:
                skill = settings.get("recognition_skill", "streetwise")
                recognized = self.resolve_action(enforcer, skill, difficulty)["success"]
        checks[key] = recognized
        return recognized

    # -- Starting a confrontation --------------------------------------------------------

    def check_enforcement(self):
        """!
        @brief Whether an enforcer here recognizes the player as wanted -- called wherever the
            scene's roster changes and when a disguise goes on or off, beside check_presence.
            A bounty under arrest_at is ignored, as is one a bribe already bought this enforcer
            off for. At kill_on_sight_at the enforcer attacks instead of talking.
        """
        if self.pending_arrest:
            return
        polity = self.current_polity()
        if not polity or not self._find_polity(polity):
            return
        arrest_at, kill_at = self._polity_enforcement(polity)
        if arrest_at is None:
            return
        for enforcer in self._present_enforcers():
            looked_away = self.entities[enforcer].get("looked_away", {})
            for identity, needs_see_through in self._presented_identities():
                bounty = self._record_bounty(polity, identity)
                if bounty <= 0 or bounty < arrest_at or looked_away.get(identity, -1) >= bounty:
                    continue
                if not self._enforcer_recognizes(enforcer, identity, polity, needs_see_through):
                    continue
                if kill_at is not None and bounty >= kill_at:
                    self._attack_on_sight(enforcer, polity, identity)
                else:
                    self._start_arrest(enforcer, polity, identity, witnessed=False)
                return

    def _enforcer_witnessed(self, enforcer, polity, identity):
        """!
        @brief enforcer just watched the player commit a crime (DM_Law.py's _record_crime) and
            acts on it at once -- no arrest_at threshold, no recognition roll: they saw it. A
            fresh crime during an open confrontation raises what's owed and repeats the demand.
        @return True if the law is now acting on it (this enforcer, or an open confrontation);
                False if this enforcer can't -- down, or already fighting the player (the
                victim of an assault), so the next one who saw it should.
        """
        if self.is_hostile(enforcer, self.player_name) or self.get_current_hp(enforcer) <= 0:
            return False
        disguise = self.entities.get(self.player_name, {}).get("disguise") or {}
        self.entities[enforcer].setdefault("enforcement_checks", {})[f"{identity}|{disguise.get('identity', '')}"] = True
        pending = self.pending_arrest
        if pending:
            if pending["identity"] == identity and pending["polity"] == polity:
                pending["witnessed"] = True
                if pending.get("announced"):
                    self._queue_arrest_announcement("repeat")
            return True
        _arrest_at, kill_at = self._polity_enforcement(polity)
        if kill_at is not None and self._record_bounty(polity, identity) >= kill_at:
            self._attack_on_sight(enforcer, polity, identity)
        else:
            self._start_arrest(enforcer, polity, identity, witnessed=True)
        return True

    def _start_arrest(self, enforcer, polity, identity, witnessed):
        self.pending_arrest = {
            "enforcer": enforcer, "polity": polity, "identity": identity, "witnessed": witnessed,
            "location": self.current_location_key, "tried": [], "strikes": 0, "announced": False,
        }
        self.event_bus.publish("log_info", f"Law: {enforcer} confronts {identity} in {polity} (bounty {self._record_bounty(polity, identity)}).")
        self._queue_arrest_announcement("arrest")

    def _attack_on_sight(self, enforcer, polity, identity):
        """!@brief Wanted past kill_on_sight_at: every enforcer present turns on the player through
            the ordinary hostility system (the "wanted_dead" attitude event)."""
        for name in self._present_enforcers():
            self.nudge_attitude_from_event(name, self.player_name, "wanted_dead", 1.0)
        self.event_bus.publish("log_info", f"Law: {enforcer} attacks {identity} on sight in {polity}.")
        self._queue_announcement("arrest_confronted", {
            **self._arrest_facts(enforcer, polity, identity), "kind": "kill_on_sight",
        })

    # -- Announcing ----------------------------------------------------------------------

    def _arrest_facts(self, enforcer, polity, identity):
        """!@brief What narration may say about a confrontation -- all of it from the record."""
        record = self.legal_records.get(polity, {}).get(identity) or {}
        bounty = record.get("bounty", 0) or 0
        charges = []
        for crime in record.get("crimes", []):
            if crime.get("settled") or crime.get("superseded"):
                continue
            victim = self._display_name(crime.get("victim"))
            charges.append(f"{crime['crime'].replace('_', ' ')}" + (f" ({victim})" if victim else ""))
        return {
            "enforcer": self._display_name(enforcer), "polity": polity,
            # Only for a disguise -- who the enforcer takes the player for.
            "addressed_as": self._identity_label(identity) if identity != self.player_name else None,
            "amount": bounty, "amount_text": self.format_currency(bounty), "charges": charges,
            "present_entities": list(self.scenario_entities),
        }

    def _queue_arrest_announcement(self, kind):
        pending = self.pending_arrest
        self._queue_announcement("arrest_confronted", {
            **self._arrest_facts(pending["enforcer"], pending["polity"], pending["identity"]),
            "kind": kind, "witnessed": pending["witnessed"],
        })

    def _queue_announcement(self, event, payload):
        """!@brief Holds an arrest event until the current input has resolved (so the crime is
            narrated first); outside an input -- a scene's first roster -- it goes out at once."""
        self._arrest_announcements.append((event, payload))
        if not self._handling_input:
            self._flush_arrest_announcements()

    def _flush_arrest_announcements(self):
        """!
        @brief Publishes the held arrest events. One the player has to answer -- the demand, or
            a failed bribe or bluff after which it stands -- carries the options notice as its
            own "notice", which LLMCore shows right after that narration rather than ahead of
            it (found by playtest: the options line came before the arrest it was about).
        """
        announcements, self._arrest_announcements = self._arrest_announcements, []
        for event, payload in announcements:
            pending = self.pending_arrest
            if event == "arrest_confronted" and payload.get("kind") == "arrest" and pending \
                    and self.is_hostile(pending["enforcer"], self.player_name):
                # Already fighting the player by the time this went out (attacked later in the
                # same input) -- there's nothing left to demand.
                self.pending_arrest = None
                continue
            awaits_reply = pending and (
                payload.get("kind") in ("arrest", "repeat") or payload.get("outcome") in ("bribe_refused", "bluff_failed")
            )
            if awaits_reply:
                payload["notice"] = self._arrest_notice()
            self.event_bus.publish(event, payload)
            if awaits_reply:
                pending["announced"] = True
                self._await_arrest_reply(notice=False)

    def _remaining_choices(self):
        tried = (self.pending_arrest or {}).get("tried", [])
        return [choice for choice in ARREST_CHOICES if choice not in tried]

    def _arrest_notice(self, reason=None):
        """!@brief The out-of-character line telling the player what they can answer."""
        pending = self.pending_arrest
        facts = self._arrest_facts(pending["enforcer"], pending["polity"], pending["identity"])
        options = ", ".join("bribe <amount>" if choice == "bribe" else choice for choice in self._remaining_choices())
        return (reason + " " if reason else "") + (
            f"{facts['enforcer']} wants {facts['amount_text']}. Reply with one of: {options}."
        )

    def _await_arrest_reply(self, reason=None, notice=True):
        """!
        @brief Tells NLPCore the next input answers the confrontation and, unless a narration is
            already carrying it (notice=False), tells the player out of character what they can
            say -- ex: right away after a reply that changed nothing ("You only have 2 gold").
        """
        self.event_bus.publish("arrest_awaiting", {"choices": self._remaining_choices()})
        if notice:
            self.event_bus.publish("player_notice", {"message": self._arrest_notice(reason), "reason": "arrest", "input": ""})

    # -- The player's reply --------------------------------------------------------------

    def _on_enforcement_input_started(self, _player_input):
        self._handling_input = True
        self._arrest_input = {"answer": None, "acted": False, "assaulted": False}

    def _note_player_acted(self, _data):
        self._arrest_input["acted"] = True

    def note_arrest_assault(self, attacker):
        """!@brief Called by DM_Law.py's note_assault -- the player attacking anyone while a
            confrontation is open is resisting it."""
        if attacker == self.player_name:
            self._arrest_input["assaulted"] = True

    def _on_arrest_answered(self, data):
        """!
        @brief NLPCore's read of the player's reply. One of ARREST_CHOICES resolves now;
            anything else ("other") is played as an ordinary input and judged once it has
            resolved (_on_player_input_handled).
        """
        choice = data.get("choice")
        if not self.pending_arrest or not self.pending_arrest.get("announced"):
            return
        if choice not in ARREST_CHOICES:
            self._arrest_input["answer"] = "other"
            return
        self._arrest_input["answer"] = choice
        handler = {
            "pay": self._arrest_pay, "surrender": self._arrest_surrender, "bribe": self._arrest_bribe,
            "bluff": self._arrest_bluff, "resist": self._arrest_resist,
        }[choice]
        handler(data.get("input", ""))

    def _on_player_input_handled(self, _data=None):
        """!
        @brief The player's input has fully resolved. An open, announced confrontation is
            judged on what they did: the enforcer dead or gone ends it; leaving the location,
            or attacking anyone, is resisting; carrying on with some other action is a strike,
            and STALL_LIMIT strikes is resisting too. Talk alone isn't a strike. Then any
            arrest events this input raised go out.
        """
        pending = self.pending_arrest
        flags = self._arrest_input
        if pending and pending.get("announced") and flags["answer"] in (None, "other"):
            enforcer = pending["enforcer"]
            if enforcer not in self.entities or self.get_current_hp(enforcer) <= 0:
                self.pending_arrest = None
                self.event_bus.publish("log_info", f"Law: the confrontation ended -- {enforcer} is down.")
            elif self.current_location_key != pending["location"]:
                self._arrest_resist("", how="fled")
            elif flags["assaulted"]:
                self._arrest_resist("", how="attacked")
            elif flags["answer"] == "other" and flags["acted"]:
                pending["strikes"] += 1
                if pending["strikes"] >= STALL_LIMIT:
                    self._arrest_resist("", how="ignored")
                else:
                    self._queue_arrest_announcement("repeat")
            elif flags["answer"] == "other":
                self._await_arrest_reply()
        self._handling_input = False
        self._arrest_input = {"answer": None, "acted": False, "assaulted": False}
        self._flush_arrest_announcements()

    # -- The five replies ----------------------------------------------------------------

    def _owed(self):
        pending = self.pending_arrest
        return self._record_bounty(pending["polity"], pending["identity"])

    def _settle_record(self, polity, identity):
        """!@brief Paid or served: the bounty goes to 0 and every charge on it is settled.
            Acclaim never decays -- the town still remembers."""
        record = self.legal_records.get(polity, {}).get(identity)
        if not record:
            return
        record["bounty"] = 0
        for crime in record.get("crimes", []):
            crime["settled"] = True

    def _resolve_arrest(self, outcome, **facts):
        pending, self.pending_arrest = self.pending_arrest, None
        self.event_bus.publish("log_info", f"Law: arrest of {pending['identity']} resolved -- {outcome}.")
        self._queue_announcement("arrest_resolved", {
            **self._arrest_facts(pending["enforcer"], pending["polity"], pending["identity"]),
            "outcome": outcome, **facts,
        })

    def _arrest_pay(self, _input_text):
        pending = self.pending_arrest
        owed = self._owed()
        purse = self.entities[self.player_name].get("currency", 0)
        if purse < owed:
            self._await_arrest_reply(
                f"You have {self.format_currency(purse)}, not the {self.format_currency(owed)} owed."
            )
            return
        self.transfer_currency(self.player_name, pending["enforcer"], owed)
        self._settle_record(pending["polity"], pending["identity"])
        self._resolve_arrest("paid", paid_text=self.format_currency(owed))

    def _jail_location(self):
        """!
        @brief Where a sentence is served: the current location's own "jail", else the first
            one up its return_to chain (a town's landmarks share the town's), else the polity's.
        @return A location key, or None (served in custody where the player stands).
        """
        key, seen = self.current_location_key, set()
        while key and key not in seen:
            seen.add(key)
            location = self.locations.get(key, {})
            if location.get("jail") in self.locations:
                return location["jail"]
            key = location.get("return_to")
        jail = (self._find_polity((self.pending_arrest or {}).get("polity")) or {}).get("jail")
        return jail if jail in self.locations else None

    def _arrest_surrender(self, _input_text):
        """!
        @brief Pays what the purse holds; the rest is served as jail time on the block clock
            ([law].jail_blocks_per_unit per unit unpaid) -- moved to the jail if there is one,
            then advance_blocks. The bounty is settled either way.
        """
        pending = self.pending_arrest
        owed = self._owed()
        purse = self.entities[self.player_name].get("currency", 0)
        paid = min(purse, owed)
        if paid > 0:
            self.transfer_currency(self.player_name, pending["enforcer"], paid)
        shortfall = _settle(owed - paid)
        blocks = Law_Resolution.jail_blocks(shortfall, self._law_settings().get("jail_blocks_per_unit", 0))
        self._settle_record(pending["polity"], pending["identity"])
        jail = self._jail_location() if blocks else None
        self._resolve_arrest(
            "surrendered", paid=paid, paid_text=self.format_currency(paid), blocks=blocks,
            hours=self._blocks_to_hours(blocks),
            jail_name=self.locations.get(jail, {}).get("name") if jail else None,
        )
        if jail and jail != self.current_location_key:
            self._enter_location(jail)
        if blocks:
            self.advance_blocks(blocks)

    def _blocks_to_hours(self, blocks):
        state = self.get_time_state()
        return round(blocks * state["hours_per_day"] / state["blocks_per_day"])

    def _arrest_bribe(self, input_text):
        """!
        @brief An offer named in the reply ("bribe him 5 gold"): the bribe skill (charisma)
            against the enforcer's resist roll (willpower) plus the [[law.bribe]] band for the
            offer's share of the bounty. Taken: the money goes to the enforcer, who looks away
            from this identity until its bounty rises -- the record itself stands. Refused (or
            incorruptible, or an insulting offer): the money stays, the enforcer is annoyed, and
            the confrontation goes on. One try per confrontation.
        """
        pending = self.pending_arrest
        if "bribe" in pending["tried"]:
            self._await_arrest_reply("You already tried a bribe.")
            return
        denominations = self.rules.get("currency", {}).get("denomination", [])
        offer = parse_currency_amount(input_text, denominations)
        if not offer:
            self._await_arrest_reply("Say how much you offer -- for example, \"bribe 5 gold\".")
            return
        purse = self.entities[self.player_name].get("currency", 0)
        if offer > purse:
            self._await_arrest_reply(f"You only have {self.format_currency(purse)}.")
            return
        pending["tried"].append("bribe")
        enforcer = pending["enforcer"]
        settings = self._law_settings()
        owed = self._owed()
        modifier = Law_Resolution.bribe_modifier(offer, owed, settings.get("bribe", []))
        if INCORRUPTIBLE_TAG in self.entities[enforcer].get("tags", []) or modifier is None:
            taken = False
        else:
            resist = self.resolve_action(enforcer, settings.get("bribe_resist_skill", "willpower"))["roll"]
            taken = self.resolve_action(
                self.player_name, settings.get("bribe_skill", "charisma"), max(0, resist + modifier),
            )["success"]
        offer_text = self.format_currency(offer)
        if taken:
            self.transfer_currency(self.player_name, enforcer, offer)
            self.entities[enforcer].setdefault("looked_away", {})[pending["identity"]] = owed
            self._resolve_arrest("bribed", offer_text=offer_text)
            return
        self.nudge_attitude_from_event(enforcer, self.player_name, "refused_bribe", 1.0)
        self._queue_announcement("arrest_resolved", {
            **self._arrest_facts(enforcer, pending["polity"], pending["identity"]),
            "outcome": "bribe_refused", "offer_text": offer_text,
        })

    def _arrest_bluff(self, _input_text):
        """!
        @brief Talking their way out: the bluff skill (trickery) against the enforcer's
            observation, harder by [law].bluff_witnessed_modifier when the enforcer saw the
            crime. Fooled: this enforcer no longer takes the player for this identity. Not
            fooled: the confrontation goes on. One try per confrontation.
        """
        pending = self.pending_arrest
        if "bluff" in pending["tried"]:
            self._await_arrest_reply("You already tried a bluff.")
            return
        pending["tried"].append("bluff")
        enforcer = pending["enforcer"]
        settings = self._law_settings()
        resist = self.resolve_action(enforcer, settings.get("bluff_resist_skill", "observation"))["roll"]
        if pending["witnessed"]:
            resist += settings.get("bluff_witnessed_modifier", 0)
        fooled = self.resolve_action(self.player_name, settings.get("bluff_skill", "trickery"), resist)["success"]
        if fooled:
            disguise = self.entities.get(self.player_name, {}).get("disguise") or {}
            key = f"{pending['identity']}|{disguise.get('identity', '')}"
            self.entities[enforcer].setdefault("enforcement_checks", {})[key] = False
            self._resolve_arrest("bluffed")
            return
        self._queue_announcement("arrest_resolved", {
            **self._arrest_facts(enforcer, pending["polity"], pending["identity"]), "outcome": "bluff_failed",
        })

    def _arrest_resist(self, _input_text, how="refused"):
        """!
        @brief Refusing, attacking, fleeing or ignoring the demand: every enforcer present --
            and the one who made it, even if the player has left -- turns hostile through the
            ordinary hostility system ("resisted_arrest"), and the polity's resisting_arrest
            law, if it has one, is filed at once (an enforcer saw it).
        """
        pending = self.pending_arrest
        for name in set(self._present_enforcers()) | {pending["enforcer"]}:
            if name in self.entities and self.get_current_hp(name) > 0:
                self.nudge_attitude_from_event(name, self.player_name, "resisted_arrest", 1.0)
        polity = self._find_polity(pending["polity"]) or {}
        laws = Law_Resolution.matching_laws(polity.get("law", []), "resisting_arrest")
        if laws:
            self._file_report({
                "polity": pending["polity"], "identity": pending["identity"], "law": laws[0],
                "line": {"crime": "resisting_arrest", "victim": None, "subject": None, "block": self.current_block},
            })
        self._resolve_arrest("fled" if how == "fled" else "resisted", how=how)
