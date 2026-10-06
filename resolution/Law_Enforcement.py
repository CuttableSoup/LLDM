"""!
@file Law_Enforcement.py
@brief LawEnforcement -- what a guard does about a crime, and the legal state that answers it
    (see docs/law.md "Enforcement"). DMCore-independent: everything it needs from the running
    game comes through one LawWorld port (below), so the whole confrontation flow can be driven
    against a fake world with no DMCore, TOML boot or EventBus.

    DM_Law.py decides who witnessed what and calls in here to file the report; this owns what
    happens next. An enforcer (tag law_enforcer) who witnesses a crime confronts the player at
    once, whatever the bounty. One who meets a wanted player later has to recognize them first --
    the same disguise-then-acclaim check witnesses use -- and only confronts a bounty of at least
    the polity's arrest_at; at kill_on_sight_at it attacks instead. Everything here is decided by
    rolls and records, never by the narrator.

    A confrontation is pending_arrest. It is announced once the player's input has finished
    resolving (on_input_handled), so the guard steps in after the crime is narrated, not before.
    NLPCore reads the next input as the reply (on_arrest_answered): pay, surrender, bribe, bluff
    or resist. Anything else is played as usual -- but attacking anyone or leaving counts as
    resisting, and carrying on with something else twice does too.

    This also owns the three pieces of save state the law keeps (legal_records, pending_reports,
    pending_arrest) and is the Persistable that round-trips them.
"""

import resolution.Combat_Resolution as Combat_Resolution
import resolution.Law_Resolution as Law_Resolution
from persistence.slot import Persistable
from resolution.Inventory_Resolution import _settle, parse_currency_amount
from resolution.World_Context import WorldContext

# An enforcer tagged this turns every bribe down without a roll.
INCORRUPTIBLE_TAG = "incorruptible"

# The replies a confrontation offers, in the order the notice lists them.
ARREST_CHOICES = ("pay", "surrender", "bribe", "bluff", "resist")

# How many times the player may carry on with something else before that counts as resisting.
STALL_LIMIT = 2


class LawWorld:
    """!
    @brief The port LawEnforcement reads and acts on the running game through. DMCore supplies
        DMCoreLawWorld (dm/DM_Enforcement.py); a test supplies a fake. Reads are plain
        attributes; every mutation of the world that isn't just an entity-dict field goes
        through one method here.

        Attributes (read): entities, rules, scenario_entities, player_name,
        current_location_key, locations, current_block.

        publish is on the port rather than returned from each method on purpose: the flows
        interleave events with world changes (surrender announces the outcome, *then* moves the
        party to jail and advances the clock), and narration reads the world as it stands when an
        event goes out, so publishing afterwards would reorder them.
    """

    def current_polity(self):
        """!@return The polity whose law applies at the current location, or None."""
        raise NotImplementedError

    def sees_through(self, witness, subject):
        """!@return Whether witness sees through subject's disguise (rolled once, remembered)."""
        raise NotImplementedError

    def is_hostile(self, entity_name, toward_name):
        raise NotImplementedError

    def is_party_member(self, entity_name):
        raise NotImplementedError

    def resolve_action(self, entity_name, skill_name, difficulty=0):
        """!@return {"success", "roll", ...} -- DMCore.resolve_action."""
        raise NotImplementedError

    def nudge_attitude(self, entity_name, toward_name, event_name, magnitude):
        raise NotImplementedError

    def transfer_currency(self, from_name, to_name, amount):
        raise NotImplementedError

    def format_currency(self, amount):
        raise NotImplementedError

    def enter_location(self, location_key):
        raise NotImplementedError

    def advance_blocks(self, blocks):
        raise NotImplementedError

    def hours_for_blocks(self, blocks):
        raise NotImplementedError

    def publish(self, event, payload):
        raise NotImplementedError


class LawEnforcement(Persistable):
    """!
    @brief See the module docstring. legal_records: {polity: {identity: {"bounty", "acclaim",
        "crimes"}}}. pending_reports: crimes witnessed but not yet filed -- each {"polity",
        "identity", "law", "line", "witnesses"} -- filed at the next advance_blocks if a witness
        lives. pending_arrest: the open confrontation, or None -- {"enforcer", "polity",
        "identity", "witnessed", "location", "tried", "strikes", "announced"}. All three save.
    """

    def __init__(self, world):
        self.world = world
        self.legal_records = {}
        self.pending_reports = []
        self.pending_arrest = None
        # Arrest events waiting for the end of the current input.
        self._announcements = []
        self._handling_input = False
        # What the current input did that a confrontation cares about.
        self._input = {"answer": None, "acted": False, "assaulted": False}

    # -- Persistable ---------------------------------------------------------------------

    def snapshot(self):
        return {
            "legal_records": self.legal_records,
            "pending_reports": self.pending_reports,
            "pending_arrest": self.pending_arrest,
        }

    def restore(self, data):
        self.legal_records = data.get("legal_records", {})
        self.pending_reports = data.get("pending_reports", [])
        self.pending_arrest = data.get("pending_arrest")

    def resume_after_load(self):
        """!@brief A reload with the guard still waiting on an answer: NLPCore needs to read the
            next input as one (a reload asks the open question again)."""
        if self.pending_arrest and self.pending_arrest.get("announced"):
            self.await_arrest_reply()

    # -- Filing reports ------------------------------------------------------------------

    def queue_report(self, report):
        """!@brief A crime no enforcer saw: held until time passes (file_pending_reports)."""
        self.pending_reports.append(report)

    def file_report(self, report):
        record = Law_Resolution.file_report(
            self.legal_records, report["polity"], report["identity"], report["law"],
            {key: report["line"].get(key) for key in ("crime", "victim", "subject", "block")},
        )
        self.world.publish("log_info", (
            f"Law: {report['line']['crime']} filed against {report['identity']} in {report['polity']} "
            f"(bounty {record['bounty']}, acclaim {record['acclaim']})."
        ))

    def file_pending_reports(self):
        """!
        @brief Called when time passes: every queued report with a witness still alive reaches
            its polity's record. Silencing every witness before time passes means the crime is
            never filed -- they still knew, but nobody lived to tell.
        """
        entities = self.world.entities
        pending, self.pending_reports = self.pending_reports, []
        for report in pending:
            if any(self._hp(name) > 0 for name in report["witnesses"] if name in entities):
                self.file_report(report)

    # -- Who enforces, and whom they know ------------------------------------------------

    def _present_enforcers(self):
        """!@brief Living enforcers in the scene who aren't already fighting the player."""
        world = self.world
        return [
            name for name in world.scenario_entities
            if Law_Resolution.is_enforcer(world.entities, name)
            and self._hp(name) > 0
            and not world.is_party_member(name) and not world.is_hostile(name, world.player_name)
        ]

    def _law_settings(self):
        return self.world.rules.get("law", {})

    def _hp(self, name):
        return Combat_Resolution.get_current_hp(WorldContext(entities=self.world.entities), name)

    def _polity_enforcement(self, polity_name):
        """!@return (arrest_at, kill_on_sight_at) for the polity -- None for either means off."""
        polity = Law_Resolution.find_polity(self.world.rules, polity_name) or {}
        return polity.get("arrest_at"), polity.get("kill_on_sight_at")

    def _record_bounty(self, polity, identity):
        return (self.legal_records.get(polity, {}).get(identity) or {}).get("bounty", 0) or 0

    def _presented_identities(self):
        """!
        @return [(identity, needs_see_through)] -- who the player shows the world: a disguise's
                own identity (seen by anyone looking), then the player beneath it, who has to be
                seen through first.
        """
        player = self.world.player_name
        disguise = self.world.entities.get(player, {}).get("disguise")
        if disguise:
            return [(disguise["identity"], False), (player, True)]
        return [(player, False)]

    def _check_key(self, identity):
        """!@brief The once-per-identity-per-disguise key enforcement_checks is stored under."""
        disguise = self.world.entities.get(self.world.player_name, {}).get("disguise") or {}
        return f"{identity}|{disguise.get('identity', '')}"

    def _enforcer_recognizes(self, enforcer, identity, polity, needs_see_through):
        """!
        @brief Whether enforcer knows the player as identity -- seen through any disguise first
            (the world's sees_through) when identity is the face beneath it, then either having
            watched that identity commit a crime, or the acclaim roll a banned presence gets
            ([law].recognition_skill against the [[law.recognition]] band). Checked once per
            enforcer per identity per disguise.
        """
        world = self.world
        key = self._check_key(identity)
        checks = world.entities[enforcer].setdefault("enforcement_checks", {})
        if key in checks:
            return checks[key]
        if needs_see_through and not world.sees_through(enforcer, world.player_name):
            recognized = False
        elif any(seen.get("offender") == identity for seen in world.entities[enforcer].get("known_crimes", [])):
            recognized = True
        else:
            settings = self._law_settings()
            subject = world.entities.get(identity, {}) if identity in world.entities else {}
            record = self.legal_records.get(polity, {}).get(identity)
            _, magnitude = Law_Resolution.effective_acclaim(subject, record)
            difficulty = Law_Resolution.recognition_difficulty(
                magnitude, settings.get("recognition", []), world.rules.get("difficulty_tier", []),
            )
            if difficulty is None:
                recognized = False
            elif difficulty == 0:
                recognized = True
            else:
                skill = settings.get("recognition_skill", "streetwise")
                recognized = world.resolve_action(enforcer, skill, difficulty)["success"]
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
        polity = self.world.current_polity()
        if not polity or not Law_Resolution.find_polity(self.world.rules, polity):
            return
        arrest_at, kill_at = self._polity_enforcement(polity)
        if arrest_at is None:
            return
        for enforcer in self._present_enforcers():
            looked_away = self.world.entities[enforcer].get("looked_away", {})
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

    def enforcer_witnessed(self, enforcer, polity, identity):
        """!
        @brief enforcer just watched the player commit a crime (DM_Law.py's _record_crime) and
            acts on it at once -- no arrest_at threshold, no recognition roll: they saw it. A
            fresh crime during an open confrontation raises what's owed and repeats the demand.
        @return True if the law is now acting on it (this enforcer, or an open confrontation);
                False if this enforcer can't -- down, or already fighting the player (the
                victim of an assault), so the next one who saw it should.
        """
        world = self.world
        if world.is_hostile(enforcer, world.player_name) \
                or self._hp(enforcer) <= 0:
            return False
        world.entities[enforcer].setdefault("enforcement_checks", {})[self._check_key(identity)] = True
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
            "location": self.world.current_location_key, "tried": [], "strikes": 0, "announced": False,
        }
        self.world.publish(
            "log_info",
            f"Law: {enforcer} confronts {identity} in {polity} (bounty {self._record_bounty(polity, identity)}).",
        )
        self._queue_arrest_announcement("arrest")

    def _attack_on_sight(self, enforcer, polity, identity):
        """!@brief Wanted past kill_on_sight_at: every enforcer present turns on the player through
            the ordinary hostility system (the "wanted_dead" attitude event)."""
        for name in self._present_enforcers():
            self.world.nudge_attitude(name, self.world.player_name, "wanted_dead", 1.0)
        self.world.publish("log_info", f"Law: {enforcer} attacks {identity} on sight in {polity}.")
        self._queue_announcement("arrest_confronted", {
            **self._arrest_facts(enforcer, polity, identity), "kind": "kill_on_sight",
        })

    # -- Announcing ----------------------------------------------------------------------

    def _arrest_facts(self, enforcer, polity, identity):
        """!@brief What narration may say about a confrontation -- all of it from the record."""
        world = self.world
        record = self.legal_records.get(polity, {}).get(identity) or {}
        bounty = record.get("bounty", 0) or 0
        charges = []
        for crime in record.get("crimes", []):
            if crime.get("settled") or crime.get("superseded"):
                continue
            victim = Law_Resolution.display_name(world.entities, crime.get("victim"))
            charges.append(f"{crime['crime'].replace('_', ' ')}" + (f" ({victim})" if victim else ""))
        return {
            "enforcer": Law_Resolution.display_name(world.entities, enforcer), "polity": polity,
            # Only for a disguise -- who the enforcer takes the player for.
            "addressed_as": (
                Law_Resolution.identity_label(world.entities, identity) if identity != world.player_name else None
            ),
            "amount": bounty, "amount_text": world.format_currency(bounty), "charges": charges,
            "present_entities": list(world.scenario_entities),
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
        self._announcements.append((event, payload))
        if not self._handling_input:
            self._flush_announcements()

    def _flush_announcements(self):
        """!
        @brief Publishes the held arrest events. One the player has to answer -- the demand, or
            a failed bribe or bluff after which it stands -- carries the options notice as its
            own "notice", which LLMCore shows right after that narration rather than ahead of
            it (found by playtest: the options line came before the arrest it was about).
        """
        world = self.world
        announcements, self._announcements = self._announcements, []
        for event, payload in announcements:
            pending = self.pending_arrest
            if event == "arrest_confronted" and payload.get("kind") == "arrest" and pending \
                    and world.is_hostile(pending["enforcer"], world.player_name):
                # Already fighting the player by the time this went out (attacked later in the
                # same input) -- there's nothing left to demand.
                self.pending_arrest = None
                continue
            awaits_reply = pending and (
                payload.get("kind") in ("arrest", "repeat") or payload.get("outcome") in ("bribe_refused", "bluff_failed")
            )
            if awaits_reply:
                payload["notice"] = self._arrest_notice()
            world.publish(event, payload)
            if awaits_reply:
                pending["announced"] = True
                self.await_arrest_reply(notice=False)

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

    def await_arrest_reply(self, reason=None, notice=True):
        """!
        @brief Tells NLPCore the next input answers the confrontation and, unless a narration is
            already carrying it (notice=False), tells the player out of character what they can
            say -- ex: right away after a reply that changed nothing ("You only have 2 gold").
        """
        self.world.publish("arrest_awaiting", {"choices": self._remaining_choices()})
        if notice:
            self.world.publish("player_notice", {"message": self._arrest_notice(reason), "reason": "arrest", "input": ""})

    # -- The player's reply --------------------------------------------------------------

    def on_input_started(self, _player_input=None):
        self._handling_input = True
        self._input = {"answer": None, "acted": False, "assaulted": False}

    def note_player_acted(self, _data=None):
        self._input["acted"] = True

    def note_assault(self, attacker):
        """!@brief The player attacking anyone while a confrontation is open is resisting it."""
        if attacker == self.world.player_name:
            self._input["assaulted"] = True

    def on_arrest_answered(self, data):
        """!
        @brief NLPCore's read of the player's reply. One of ARREST_CHOICES resolves now;
            anything else ("other") is played as an ordinary input and judged once it has
            resolved (on_input_handled).
        """
        choice = data.get("choice")
        if not self.pending_arrest or not self.pending_arrest.get("announced"):
            return
        if choice not in ARREST_CHOICES:
            self._input["answer"] = "other"
            return
        self._input["answer"] = choice
        handler = {
            "pay": self._arrest_pay, "surrender": self._arrest_surrender, "bribe": self._arrest_bribe,
            "bluff": self._arrest_bluff, "resist": self._arrest_resist,
        }[choice]
        handler(data.get("input", ""))

    def on_input_handled(self, _data=None):
        """!
        @brief The player's input has fully resolved. An open, announced confrontation is
            judged on what they did: the enforcer dead or gone ends it; leaving the location,
            or attacking anyone, is resisting; carrying on with some other action is a strike,
            and STALL_LIMIT strikes is resisting too. Talk alone isn't a strike. Then any
            arrest events this input raised go out.
        """
        world = self.world
        pending = self.pending_arrest
        flags = self._input
        if pending and pending.get("announced") and flags["answer"] in (None, "other"):
            enforcer = pending["enforcer"]
            if enforcer not in world.entities or self._hp(enforcer) <= 0:
                self.pending_arrest = None
                world.publish("log_info", f"Law: the confrontation ended -- {enforcer} is down.")
            elif world.current_location_key != pending["location"]:
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
                self.await_arrest_reply()
        self._handling_input = False
        self._input = {"answer": None, "acted": False, "assaulted": False}
        self._flush_announcements()

    # -- The five replies ----------------------------------------------------------------

    def _owed(self):
        pending = self.pending_arrest
        return self._record_bounty(pending["polity"], pending["identity"])

    def _purse(self):
        return self.world.entities[self.world.player_name].get("currency", 0)

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
        self.world.publish("log_info", f"Law: arrest of {pending['identity']} resolved -- {outcome}.")
        self._queue_announcement("arrest_resolved", {
            **self._arrest_facts(pending["enforcer"], pending["polity"], pending["identity"]),
            "outcome": outcome, **facts,
        })

    def _arrest_pay(self, _input_text):
        world = self.world
        pending = self.pending_arrest
        owed = self._owed()
        purse = self._purse()
        if purse < owed:
            self.await_arrest_reply(
                f"You have {world.format_currency(purse)}, not the {world.format_currency(owed)} owed."
            )
            return
        world.transfer_currency(world.player_name, pending["enforcer"], owed)
        self._settle_record(pending["polity"], pending["identity"])
        self._resolve_arrest("paid", paid_text=world.format_currency(owed))

    def jail_location(self):
        """!
        @brief Where a sentence is served: the current location's own "jail", else the first
            one up its return_to chain (a town's landmarks share the town's), else the polity's.
        @return A location key, or None (served in custody where the player stands).
        """
        world = self.world
        key, seen = world.current_location_key, set()
        while key and key not in seen:
            seen.add(key)
            location = world.locations.get(key, {})
            if location.get("jail") in world.locations:
                return location["jail"]
            key = location.get("return_to")
        polity = Law_Resolution.find_polity(world.rules, (self.pending_arrest or {}).get("polity")) or {}
        jail = polity.get("jail")
        return jail if jail in world.locations else None

    def _arrest_surrender(self, _input_text):
        """!
        @brief Pays what the purse holds; the rest is served as jail time on the block clock
            ([law].jail_blocks_per_unit per unit unpaid) -- moved to the jail if there is one,
            then the clock advances. The bounty is settled either way.
        """
        world = self.world
        pending = self.pending_arrest
        owed = self._owed()
        paid = min(self._purse(), owed)
        if paid > 0:
            world.transfer_currency(world.player_name, pending["enforcer"], paid)
        shortfall = _settle(owed - paid)
        blocks = Law_Resolution.jail_blocks(shortfall, self._law_settings().get("jail_blocks_per_unit", 0))
        self._settle_record(pending["polity"], pending["identity"])
        jail = self.jail_location() if blocks else None
        self._resolve_arrest(
            "surrendered", paid=paid, paid_text=world.format_currency(paid), blocks=blocks,
            hours=world.hours_for_blocks(blocks),
            jail_name=world.locations.get(jail, {}).get("name") if jail else None,
        )
        if jail and jail != world.current_location_key:
            world.enter_location(jail)
        if blocks:
            world.advance_blocks(blocks)

    def _arrest_bribe(self, input_text):
        """!
        @brief An offer named in the reply ("bribe him 5 gold"): the bribe skill (charisma)
            against the enforcer's resist roll (willpower) plus the [[law.bribe]] band for the
            offer's share of the bounty. Taken: the money goes to the enforcer, who looks away
            from this identity until its bounty rises -- the record itself stands. Refused (or
            incorruptible, or an insulting offer): the money stays, the enforcer is annoyed, and
            the confrontation goes on. One try per confrontation.
        """
        world = self.world
        pending = self.pending_arrest
        if "bribe" in pending["tried"]:
            self.await_arrest_reply("You already tried a bribe.")
            return
        denominations = world.rules.get("currency", {}).get("denomination", [])
        offer = parse_currency_amount(input_text, denominations)
        if not offer:
            self.await_arrest_reply("Say how much you offer -- for example, \"bribe 5 gold\".")
            return
        purse = self._purse()
        if offer > purse:
            self.await_arrest_reply(f"You only have {world.format_currency(purse)}.")
            return
        pending["tried"].append("bribe")
        enforcer = pending["enforcer"]
        settings = self._law_settings()
        owed = self._owed()
        modifier = Law_Resolution.bribe_modifier(offer, owed, settings.get("bribe", []))
        if INCORRUPTIBLE_TAG in world.entities[enforcer].get("tags", []) or modifier is None:
            taken = False
        else:
            resist = world.resolve_action(enforcer, settings.get("bribe_resist_skill", "willpower"))["roll"]
            taken = world.resolve_action(
                world.player_name, settings.get("bribe_skill", "charisma"), max(0, resist + modifier),
            )["success"]
        offer_text = world.format_currency(offer)
        if taken:
            world.transfer_currency(world.player_name, enforcer, offer)
            world.entities[enforcer].setdefault("looked_away", {})[pending["identity"]] = owed
            self._resolve_arrest("bribed", offer_text=offer_text)
            return
        world.nudge_attitude(enforcer, world.player_name, "refused_bribe", 1.0)
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
        world = self.world
        pending = self.pending_arrest
        if "bluff" in pending["tried"]:
            self.await_arrest_reply("You already tried a bluff.")
            return
        pending["tried"].append("bluff")
        enforcer = pending["enforcer"]
        settings = self._law_settings()
        resist = world.resolve_action(enforcer, settings.get("bluff_resist_skill", "observation"))["roll"]
        if pending["witnessed"]:
            resist += settings.get("bluff_witnessed_modifier", 0)
        fooled = world.resolve_action(world.player_name, settings.get("bluff_skill", "trickery"), resist)["success"]
        if fooled:
            world.entities[enforcer].setdefault("enforcement_checks", {})[self._check_key(pending["identity"])] = False
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
        world = self.world
        pending = self.pending_arrest
        for name in set(self._present_enforcers()) | {pending["enforcer"]}:
            if name in world.entities and self._hp(name) > 0:
                world.nudge_attitude(name, world.player_name, "resisted_arrest", 1.0)
        polity = Law_Resolution.find_polity(world.rules, pending["polity"]) or {}
        laws = Law_Resolution.matching_laws(polity.get("law", []), "resisting_arrest")
        if laws:
            self.file_report({
                "polity": pending["polity"], "identity": pending["identity"], "law": laws[0],
                "line": {"crime": "resisting_arrest", "victim": None, "subject": None, "block": world.current_block},
            })
        self._resolve_arrest("fled" if how == "fled" else "resisted", how=how)
