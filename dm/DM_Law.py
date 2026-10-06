"""!
@file DM_Law.py
@brief LawMixin -- crimes, witnesses, disguise and polity records (see docs/law.md).

    What an NPC knows about a crime is game state, never something the narrator infers from its
    own scrollback: a witness gets a "known_crimes" entry, an enforcer reads its polity's record,
    and describe_character (DM_Social.py) builds their persona lines from exactly that. Anyone
    else knows nothing.

    The flow: report_crime (called from the existing theft/assault/kill/cast/presence hook
    points, or by the "report_crime" program op) finds the laws in force here, who saw it, and
    who each witness thinks did it (a disguise hides the offender unless the witness's
    observation beats it). Each witness learns it at once; the polity's record is filed at once
    if an enforcer saw it, else at the next advance_blocks if any witness is still alive.

    Records are per polity and only ever change for a crime committed inside that polity. No
    polity here means no law: nothing is recorded.
"""

from dm.DM_Types import DMCoreProtocol
import resolution.Law_Resolution as Law_Resolution

# An entity tagged this enforces the law of whatever polity it stands in: a crime it witnesses
# is filed immediately, and it knows that polity's records.
ENFORCER_TAG = "law_enforcer"

# Per-entity law state, round-tripped by DM_Persistence.py exactly as stored: what a witness
# saw (and which disguises/presences it already checked), who struck a victim first, and a
# disguise currently worn.
# Also, for an enforcer (DM_Enforcement.py): whom it has already recognized or failed to, and
# whom a bribe bought it off from.
LAW_INSTANCE_FIELDS = (
    "known_crimes", "assaulted_by", "disguise", "disguise_count", "disguise_checks", "presence_checks",
    "enforcement_checks", "looked_away",
)

# How a crime reads in a witness's own persona line (legal_facts_for).
CRIME_PHRASES = {
    "theft": "steal from {victim}",
    "assault": "attack {victim}",
    "murder": "kill {victim}",
    "banned_ability": "cast {subject}, which is forbidden here",
    "banned_presence": "bring {subject} here, which is forbidden",
}
# The same, from the victim's own side ("Was robbed by a disguised stranger.").
VICTIM_PHRASES = {"theft": "robbed", "assault": "attacked"}


class LawMixin(DMCoreProtocol):

    def _init_law_state(self):
        """!
        @brief legal_records: {polity: {identity: {"bounty", "acclaim", "crimes"}}}.
            pending_reports: crimes witnessed but not yet filed -- each {"polity", "identity",
            "law", "line", "witnesses"} -- filed at the next advance_blocks if a witness lives.
            Both round-trip through save/load (DM_Persistence.py).
        """
        self.legal_records = {}
        self.pending_reports = []
        self.event_bus.subscribe("crime_committed", self._on_crime_committed)
        self.event_bus.subscribe("disguise_changed", self._on_disguise_changed)

    # -- Where we are, and what the law is here ------------------------------------------

    def current_polity(self):
        """!
        @brief The polity whose law applies at the current location: the location's own
            "polity" field, else whichever world_map region contains its grid point, else the
            same for the location it returns to (return_to), and so on up.
        @return The polity name, or None (no law applies).
        """
        key, seen = self.current_location_key, set()
        while key and key not in seen:
            # A landmark with neither (a town's shop) is under its town's law. Found by building
            # enforcement: Sandpoint's garrison, and every other landmark, had none.
            seen.add(key)
            location = self.locations.get(key, {})
            if location.get("polity"):
                return location["polity"]
            grid = location.get("grid")
            if grid:
                return self._resolve_region_polity(grid["x"], grid["y"])
            key = location.get("return_to")
        return None

    def current_laws(self):
        """!
        @return (polity_name, laws) -- the polity's own [[polity.law]] list merged with the
                location's own [[location.law]] (Law_Resolution.merge_laws). (None, []) when no
                known polity applies.
        """
        polity_name = self.current_polity()
        polity = self._find_polity(polity_name) if polity_name else None
        if not polity:
            return None, []
        location = self.locations.get(self.current_location_key, {})
        return polity_name, Law_Resolution.merge_laws(polity.get("law", []), location.get("law", []))

    def _law_settings(self):
        return self.rules.get("law", {})

    # -- Witnesses and identity ----------------------------------------------------------

    def _offender_side(self, offender):
        """!@brief The offender plus, for a party member, every present party member -- none of
            them will report the party's own crimes."""
        if self._is_party_member(offender):
            return {name for name in self.scenario_entities if self._is_party_member(name)} | {offender}
        return {offender}

    def _witnesses(self, offender, victim=None):
        """!
        @brief Who present could report a crime by offender: Law_Resolution.is_capable_witness
            (alive, speaks, not authored hostile to the offender), never the offender's own side.
            The victim counts even after turning hostile -- being robbed is exactly what made them.
        """
        side = self._offender_side(offender)
        witnesses = []
        for name in self.scenario_entities:
            if name in side:
                continue
            if Law_Resolution.is_capable_witness(self.entities, name, offender):
                witnesses.append(name)
            elif name == victim and Law_Resolution.is_capable_witness(self.entities, name, None):
                witnesses.append(name)
        return witnesses

    def _sees_through(self, witness, subject):
        """!
        @brief Whether witness sees through subject's disguise -- the witness's own observation
            roll against the disguise's quality. Rolled once per witness per disguise and
            remembered, so standing in a crowd for ten turns doesn't mean ten chances to be made.
        @return True when subject isn't disguised at all.
        """
        disguise = self.entities.get(subject, {}).get("disguise")
        if not disguise:
            return True
        checks = self.entities[witness].setdefault("disguise_checks", {})
        if disguise["identity"] not in checks:
            skill = self._law_settings().get("witness_skill", "observation")
            checks[disguise["identity"]] = self.resolve_action(witness, skill, disguise.get("quality", 0))["success"]
        return checks[disguise["identity"]]

    def _identity_seen_by(self, witness, offender):
        """!@return (identity, described_as) -- who witness will say did it: the offender, or
            their disguise if witness didn't see through it."""
        disguise = self.entities.get(offender, {}).get("disguise")
        if disguise and not self._sees_through(witness, offender):
            return disguise["identity"], disguise["alias"]
        return offender, self.entities.get(offender, {}).get("name", offender)

    def _is_enforcer(self, name):
        return ENFORCER_TAG in self.entities.get(name, {}).get("tags", [])

    # -- Recording crimes ----------------------------------------------------------------

    def report_crime(self, crime, offender, victim=None, subject=None):
        """!
        @brief A crime just happened here. Finds the law it breaks and who saw it, then records
            it (_record_crime). A no-op with no polity, no matching law, or nobody to see it.
        @param crime One of Law_Resolution.CRIMES.
        @param offender Who did it.
        @param victim Who it was done to, if anyone (theft/assault/murder).
        @param subject The entity name a law's "match" is checked against (a spell, a creature).
        @return The witnesses' names (empty if nothing was recorded).
        """
        if victim and self.entities.get(victim, {}).get("supertype") == "object":
            # Emptying a dungeon chest isn't theft -- only taking from a living person is.
            return []
        polity, laws = self.current_laws()
        if not polity:
            return []
        subject_entity = self.entities.get(subject) if subject else None
        broken = Law_Resolution.matching_laws(laws, crime, subject_entity)
        if not broken:
            return []
        witnesses = self._witnesses(offender, victim)
        self._record_crime(polity, broken[0], crime, offender, witnesses, victim=victim, subject=subject)
        return witnesses

    def _record_crime(self, polity, law, crime, offender, witnesses, victim=None, subject=None, subject_label=None):
        """!
        @brief Each witness learns the crime (as committed by whoever they think did it), and a
            report per identity is filed now (an enforcer saw it) or queued for the next block.
        """
        if not witnesses:
            return
        by_identity = {}
        for witness in witnesses:
            identity, described_as = self._identity_seen_by(witness, offender)
            line = {
                "crime": crime, "offender": identity, "described_as": described_as,
                "victim": victim, "subject": subject_label or subject, "polity": polity,
                "block": self.current_block,
            }
            self.entities[witness].setdefault("known_crimes", []).append(line)
            by_identity.setdefault(identity, (line, []))[1].append(witness)
            if witness != victim:
                # Knowing isn't enough -- the witness's own attitude toward whoever is standing
                # there moves too (fear, coldness), so the reaction is mechanical, not left to
                # the narrator. Toward the person present even through a disguise: that's who
                # they just watched do it. The victim already got theft/assaulted.
                severity = self._law_settings().get("witness_severity", {}).get(crime, 0)
                self.nudge_attitude_from_event(witness, offender, "witnessed_crime", severity)

        for identity, (line, seen_by) in by_identity.items():
            report = {"polity": polity, "identity": identity, "law": law, "line": line, "witnesses": seen_by}
            enforcers = [name for name in seen_by if self._is_enforcer(name)]
            if enforcers:
                self._file_report(report)
                if offender == self.player_name:
                    # An enforcer who saw it acts on it now (DM_Enforcement.py) -- the first who
                    # can: one who is the victim is already fighting back.
                    for enforcer in enforcers:
                        if self._enforcer_witnessed(enforcer, polity, identity):
                            break
            else:
                self.pending_reports.append(report)

        self.event_bus.publish("crime_witnessed", {
            "crime": crime, "offender": offender, "victim": victim, "polity": polity,
            "witnesses": list(witnesses),
        })
        self.event_bus.publish("log_info", f"Law: {crime} by {offender} in {polity}, seen by {', '.join(witnesses)}.")

    def _file_report(self, report):
        record = Law_Resolution.file_report(
            self.legal_records, report["polity"], report["identity"], report["law"],
            {key: report["line"].get(key) for key in ("crime", "victim", "subject", "block")},
        )
        self.event_bus.publish("log_info", (
            f"Law: {report['line']['crime']} filed against {report['identity']} in {report['polity']} "
            f"(bounty {record['bounty']}, acclaim {record['acclaim']})."
        ))

    def _file_pending_reports(self):
        """!
        @brief Called by advance_blocks (DM_Time.py): every queued report with a witness still
            alive reaches its polity's record. Silencing every witness before time passes means
            the crime is never filed -- they still knew, but nobody lived to tell.
        """
        pending, self.pending_reports = self.pending_reports, []
        for report in pending:
            if any(self.get_current_hp(name) > 0 for name in report["witnesses"] if name in self.entities):
                self._file_report(report)

    # -- Hook points ---------------------------------------------------------------------

    def _on_crime_committed(self, data):
        """!@brief The "report_crime" program op (Program_Interpreter.py) -- ex: a fumbled
            sleight of hand."""
        if data.get("offender"):
            self.report_crime(data["crime"], data["offender"], victim=data.get("victim"))

    def note_assault(self, attacker, victim):
        """!
        @brief attacker struck victim, who wasn't hostile -- an assault, and a mark on the
            victim so a later death at the attacker's side's hands is murder (note_kill) even
            though the victim was fighting back by then.
        """
        marks = self.entities.get(victim, {}).setdefault("assaulted_by", [])
        if attacker not in marks:
            marks.append(attacker)
        self.note_arrest_assault(attacker)
        self.report_crime("assault", attacker, victim=victim)

    def note_kill(self, killer, victim):
        """!@brief victim died at killer's hand -- murder if killer's side started it (note_assault)."""
        marks = set(self.entities.get(victim, {}).get("assaulted_by", []))
        if killer in self.entities and marks & self._offender_side(killer):
            self.report_crime("murder", killer, victim=victim)

    def observe_ability_use(self, caster, ability):
        """!
        @brief caster just cast ability (a spell) in view. Each witness tries to identify it
            with the lore skill covering spells (arcane's lore_types) against
            [law].spell_identify_base + the spell's own "level"; one who can't knows only that
            *a* spell was cast -- a crime only where a law bans every spell.
        """
        if not ability or ability.get("supertype") != "spell":
            return
        polity, laws = self.current_laws()
        bans = [law for law in laws if law.get("crime") == "banned_ability"]
        if not polity or not bans:
            return
        name = ability.get("name")
        skill = self._resolve_lore_skill(name) if name in self.entities else None
        difficulty = self._law_settings().get("spell_identify_base", 15) + (ability.get("level") or 0)
        unidentified = {"supertype": "spell"}
        groups = {}
        for witness in self._witnesses(caster):
            identified = bool(skill) and self.resolve_action(witness, skill, difficulty)["success"]
            broken = Law_Resolution.matching_laws(bans, "banned_ability", ability if identified else unidentified)
            if broken:
                label = name if identified else "a spell"
                groups.setdefault((id(broken[0]), label), (broken[0], []))[1].append(witness)
        for (_, label), (law, witnesses) in groups.items():
            self._record_crime(polity, law, "banned_ability", caster, witnesses, subject=name, subject_label=label)

    def check_presence(self):
        """!
        @brief Whether anyone here recognizes a banned presence in the party -- a party member
            (or something one has equipped) matching a banned_presence law. Each witness first
            has to see through a disguise, then recognize the subject: the lore skill covering
            it (ex: miracles for undead), else [law].recognition_skill, against a difficulty set
            by its acclaim magnitude (Law_Resolution.recognition_difficulty). Checked once per
            witness per subject per disguise.
        """
        polity, laws = self.current_laws()
        bans = [law for law in laws if law.get("crime") == "banned_presence"]
        if not polity or not bans:
            return
        settings = self._law_settings()
        tiers = self.rules.get("difficulty_tier", [])
        for bearer in [name for name in self.scenario_entities if self._is_party_member(name)]:
            bearer_entity = self.entities.get(bearer, {})
            subjects = [bearer] + [item for item in bearer_entity.get("equipped", {}).values() if item in self.entities]
            for subject in subjects:
                broken = Law_Resolution.matching_laws(bans, "banned_presence", self.entities[subject])
                if not broken:
                    continue
                disguise = bearer_entity.get("disguise") or {}
                key = f"{subject}|{disguise.get('identity', '')}"
                record = self.legal_records.get(polity, {}).get(subject)
                _, magnitude = Law_Resolution.effective_acclaim(self.entities[subject], record)
                difficulty = Law_Resolution.recognition_difficulty(magnitude, settings.get("recognition", []), tiers)
                recognized = []
                for witness in self._witnesses(bearer):
                    checks = self.entities[witness].setdefault("presence_checks", {})
                    if key in checks:
                        continue
                    checks[key] = self._recognizes(witness, bearer, subject, difficulty, settings)
                    if checks[key]:
                        recognized.append(witness)
                self._record_crime(polity, broken[0], "banned_presence", bearer, recognized, subject=subject)

    def _recognizes(self, witness, bearer, subject, difficulty, settings):
        if difficulty is None or not self._sees_through(witness, bearer):
            return False
        if difficulty == 0:
            return True
        skill = self._resolve_lore_skill(subject) or settings.get("recognition_skill", "streetwise")
        return self.resolve_action(witness, skill, difficulty)["success"]

    def _on_disguise_changed(self, data):
        """!
        @brief A disguise went on or came off. A disguise made from a roll that never happened
            (a "trivial" rating skips the dice, leaving quality 0) is rolled here instead, so it
            isn't transparent to everyone. Then the room gets a fresh look at the party.
        """
        entity = self.entities.get(data.get("entity"), {})
        disguise = entity.get("disguise")
        if disguise and not disguise.get("quality"):
            skill = self._law_settings().get("disguise_skill", "disguise")
            disguise["quality"] = self.resolve_action(data["entity"], skill, 0)["roll"]
        self.check_presence()
        self.check_enforcement()

    # -- What prompts may say ------------------------------------------------------------

    def legal_facts_for(self, entity_name):
        """!
        @brief The legal facts entity_name actually holds, as persona lines: crimes it saw
            (known_crimes), and -- for an enforcer -- who is wanted in the polity it stands in.
            The *only* route by which crime knowledge reaches an NPC's prompt.
        @return A list of sentences, empty for an NPC who knows nothing.
        """
        entity = self.entities.get(entity_name, {})
        lines = []
        for seen in entity.get("known_crimes", []):
            if seen.get("victim") == entity_name and seen["crime"] in VICTIM_PHRASES:
                lines.append(f"Was {VICTIM_PHRASES[seen['crime']]} by {seen['described_as']}.")
                continue
            phrase = CRIME_PHRASES.get(seen["crime"], seen["crime"]).format(
                victim=self._display_name(seen.get("victim")) or "someone",
                subject=self._display_name(seen.get("subject")) or "something",
            )
            lines.append(f"Saw {seen['described_as']} {phrase}.")
        if self._is_enforcer(entity_name):
            polity = self.current_polity()
            for identity, record in self.legal_records.get(polity, {}).items():
                if record.get("bounty", 0) > 0:
                    lines.append(
                        f"Knows {self._identity_label(identity)} is wanted in {polity} "
                        f"(bounty {self.format_currency(record['bounty'])})."
                    )
        return lines

    def _display_name(self, name):
        if not name:
            return None
        return self.entities.get(name, {}).get("name", name)

    def _identity_label(self, identity):
        """!@brief A record identity as an NPC would name it -- a disguise reads as its alias."""
        if identity in self.entities:
            return self._display_name(identity)
        for entity in self.entities.values():
            disguise = entity.get("disguise")
            if disguise and disguise.get("identity") == identity:
                return disguise["alias"]
        return "a disguised stranger"
