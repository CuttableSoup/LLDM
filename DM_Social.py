from DM_Types import DMCoreProtocol

# nudge_attitude's own scaling factor -- the disposition delta for one dialogue turn is
# classify_sentiment's own confidence score (already a 0..1 measure of how strongly the model
# read the line as negative/positive) times this, not a flat per-sentiment amount. A plainer
# "how intense should talk-driven drift be" knob than tuning a whole delta table by hand; left
# at 1 for now (no rescaling at all) rather than tuned against real play yet. See CLAUDE.md's
# "Dialogue sentiment" for the fuller rationale.
SENTIMENT_INTENSITY_SCALE = 1
# The max |value| pure talk can accumulate on one axis toward one entity, independent of
# whatever that axis's own hand-authored base value already is. This is a real cap on drift, not
# a cap on the resolved value -- a base already close to a threshold (ex: is_hostile's -100) can
# still be pushed across it by sustained same-direction dialogue. Intentional: talking someone
# into (or out of) a fight is a real tabletop outcome, not a bug -- see CLAUDE.md's "Dialogue".
TALK_ATTITUDE_DRIFT_CAP = 40


class SocialMixin(DMCoreProtocol):
    """!
    @brief Attitudes and character/flavor-text description (DMCore mixin -- only ever composed
        into DMCore, never instantiated on its own; relies on self.entities/self.rules,
        set up by DMCore.__init__). Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes -- see DM_Types.py.
    """

    def get_attitude(self, entity_name, toward_name):
        """!
        @brief Resolves entity_name's six-value attitude array toward toward_name: a specific
            name override, then a supertype override, then the entity's default -- plus
            whatever runtime drift nudge_attitude has accumulated toward toward_name
            specifically (entity["attitude_deltas"], added elementwise on top). The static
            override/default array itself is never mutated -- for a hand-authored entity it's
            the literal TOML-sourced list object, shared across every call, so this always
            returns a fresh list rather than editing that one in place.
        @param entity_name The name of the entity whose attitude is being read.
        @param toward_name The name of the entity being regarded.
        @return The [disposition, trust, confidence, respect, obligation, intimacy] attitude array.
        """
        entity = self.entities.get(entity_name, {})
        attitudes = entity.get("attitudes", {})

        base = None
        for override in attitudes.get("name", []):
            if toward_name in override:
                base = override[toward_name]
                break

        if base is None:
            toward_supertype = self.entities.get(toward_name, {}).get("supertype")
            for override in attitudes.get("supertype", []):
                if toward_supertype in override:
                    base = override[toward_supertype]
                    break

        if base is None:
            base = attitudes.get("default", [0, 0, 0, 0, 0, 0])

        deltas = entity.get("attitude_deltas", {}).get(toward_name)
        if not deltas:
            return list(base)
        return [value + delta for value, delta in zip(base, deltas)]

    def nudge_attitude(self, entity_name, toward_name, sentiment, score):
        """!
        @brief Applies a small, capped, persistent drift to entity_name's own disposition
            toward toward_name, driven by the tone of something toward_name just said to it --
            called from DM_Dialogue.py's _resolve_dialogue, right before persona/attitude are
            read, so this turn's own reply already reflects the drift. The magnitude is score
            itself (classify_sentiment's own confidence, already 0..1) times
            SENTIMENT_INTENSITY_SCALE, not a flat per-sentiment amount -- a line the classifier
            read as more intensely negative/positive moves disposition further than a mild one.
            A no-op for None/unrecognized sentiment (there's no "neutral" case to scale -- see
            NLP_Core.py's classify_sentiment, backed by a fine-tuned sentiment classifier), a
            falsy score, or a missing entity.
        @param entity_name The entity whose attitude is drifting.
        @param toward_name The entity it's drifting toward (ex: self.player_name).
        @param sentiment "negative", "positive", or None/anything else (no-op).
        @param score classify_sentiment's own confidence in sentiment (0..1) -- the raw
            magnitude before SENTIMENT_INTENSITY_SCALE is applied.
        """
        if sentiment not in ("negative", "positive") or not score:
            return
        entity = self.entities.get(entity_name)
        if entity is None:
            return

        signed_delta = (score if sentiment == "positive" else -score) * SENTIMENT_INTENSITY_SCALE
        deltas = entity.setdefault("attitude_deltas", {})
        axis_deltas = deltas.setdefault(toward_name, [0, 0, 0, 0, 0, 0])
        axis_deltas[0] = max(-TALK_ATTITUDE_DRIFT_CAP, min(TALK_ATTITUDE_DRIFT_CAP, axis_deltas[0] + signed_delta))

    def is_hostile(self, entity_name, toward_name):
        """!
        @brief Whether entity_name is hostile enough toward toward_name to be treated as a
            combat target rather than a dialogue partner. Two distinct defaults, deliberately
            not collapsed into one:
            - No `[entity.attitudes]` table at all (ex: arena.toml's wolf/field.toml's bandit,
              which declare no attitude data whatsoever) -- treated as hostile unconditionally. A
              monster that never bothered to author a disposition is still a monster; this is
              what keeps every existing hostile creature fighting exactly as before.
            - `[entity.attitudes]` *is* declared -- disposition has to actually reach true
              hostility (<= -100) to fight. A merely wary/negative-but-not-murderous
              disposition (ex: -40) is dialogue, not combat -- "you can dislike someone and
              not be hostile." This is what a generated NPC's own resolved disposition (see
              NPC_Generation.py's variance) is checked against.
            Inanimate objects (ex: a locked chest) are never hostile regardless of attitude
            data -- they have no combat intent, so a lockpicking attempt against one must not
            get batched into "round_resolved".
        @param entity_name The name of the entity being checked.
        @param toward_name The name of the entity it might be hostile toward.
        @return True if entity_name has no attitude data at all, or its disposition (the
            attitude array's first value) is -100 or lower.
        """
        entity = self.entities.get(entity_name, {})
        if entity.get("supertype") == "object":
            return False
        if "attitudes" not in entity:
            return True
        disposition = self.get_attitude(entity_name, toward_name)[0]
        return disposition <= -100

    def get_attitude_tier(self, value):
        """!
        @brief Finds the [[attitude_tier]] definition (rules.toml) whose minimum/maximum range
            contains a single attitude axis value, clamped to [-150, 150] first -- headroom
            past the nominal -100..100 range for whenever attitudes get modified at runtime
            (nothing does yet), so an extreme value still resolves to the correct outermost
            tier instead of matching nothing. Tiers are checked in TOML declaration order and
            the first match wins, same convention as choose_behavior -- ex: a value of exactly
            -100 sits on both "hostile"'s and "unfriendly"'s boundary, and resolves to
            whichever is declared first (hostile).
        @param value A single attitude axis value (ex: disposition).
        @return The matching attitude_tier definition, or None if none match (ex: no
                [[attitude_tier]] data is loaded at all).
        """
        clamped = max(-150, min(150, value))
        for tier in self.rules.get("attitude_tier", []):
            if tier.get("minimum", float("-inf")) <= clamped <= tier.get("maximum", float("inf")):
                return tier
        return None

    def describe_attitude(self, entity_name, toward_name):
        """!
        @brief Translates entity_name's six-value attitude array toward toward_name (from
            get_attitude) into prose via [[attitude_tier]] -- one phrase per axis, banded by
            get_attitude_tier, rather than handing the LLM raw numbers it has no way to
            calibrate ("38 disposition" means nothing to a language model; "is warm and
            well-disposed toward them" does).
        @param entity_name The name of the entity whose attitude is being described.
        @param toward_name The name of the entity it's directed toward.
        @return A prose fragment ("Attitude toward X: ..."), or "" if no attitude_tier data is
                loaded (ex: a malformed rules.toml -- see load_rules' per-file try/except note).
        """
        axes = ("disposition", "trust", "confidence", "respect", "obligation", "intimacy")
        values = self.get_attitude(entity_name, toward_name)
        phrases = []
        for axis, value in zip(axes, values):
            tier = self.get_attitude_tier(value)
            if tier and tier.get(axis):
                phrases.append(tier[axis])

        if not phrases:
            return ""
        return f"Attitude toward {toward_name}: " + ", ".join(phrases) + "."

    def describe_character(self, entity_name, toward_name=None):
        """!
        @brief Builds a flavor-text description of an entity for narration prompts, out of its
            purely descriptive data (description, qualities, memories, quotes) rather than
            mechanical data (skills/dice), since this is meant to tell the LLM who someone is.
        @param entity_name The name of the entity to describe.
        @param toward_name If given (and different from entity_name), appends
            describe_attitude(entity_name, toward_name) as an additional part -- ex: passing
            self.player_name lets a "pure mechanics" entity like wolf, which otherwise has no
            descriptive data at all, still surface something to the LLM (how hostile it is),
            rather than contributing nothing to the roster/defender_details.
        @return A formatted description string, or "" if the entity has no descriptive data
                (and no attitude phrase was added). Leads with the entity's own "name" field
                rather than entity_name (its self.entities dict key) when the two differ --
                every hand-authored template has "name" == its own dict key by construction,
                so this only actually changes anything for a generated NPC (DM_NpcGeneration.py),
                whose LLM-invented name would otherwise never reach this text at all.
        """
        entity = self.entities.get(entity_name, {})
        parts = []

        description = entity.get("description")
        if description:
            parts.append(description)

        qualities = entity.get("qualities")
        if qualities:
            parts.append("Qualities: " + ", ".join(f"{key} {value}" for key, value in qualities.items()))

        memories = entity.get("memories")
        if memories:
            parts.append("Memories: " + "; ".join(memories))

        quotes = entity.get("quotes")
        if quotes:
            parts.append("Known to say: " + "; ".join(f"\"{quote}\"" for quote in quotes))

        if toward_name and toward_name != entity_name:
            attitude = self.describe_attitude(entity_name, toward_name)
            if attitude:
                parts.append(attitude)

        if not parts:
            return ""
        return f"{entity.get('name', entity_name)} - " + " | ".join(parts)
