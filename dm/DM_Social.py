import resolution.Social_Resolution as Social_Resolution
from dm.DM_Types import DMCoreProtocol
from resolution.Social_Resolution import ACTION_ATTITUDE_DRIFT_CAP, ATTITUDE_AXES, TALK_ATTITUDE_DRIFT_CAP

# nudge_attitude's own scaling factor -- the disposition delta for one dialogue turn is
# classify_sentiment's own confidence score (already a 0..1 measure of how strongly the model
# read the line as negative/positive) times this, not a flat per-sentiment amount. A plainer
# "how intense should talk-driven drift be" knob than tuning a whole delta table by hand; left
# at 1 for now (no rescaling at all) rather than tuned against real play yet. See CLAUDE.md's
# "Dialogue sentiment" for the fuller rationale.
SENTIMENT_INTENSITY_SCALE = 1
# TALK_ATTITUDE_DRIFT_CAP/ACTION_ATTITUDE_DRIFT_CAP/ATTITUDE_AXES now live in Social_Resolution.py
# (imported above) -- the pure module Program_Interpreter.py's own "attitude" op reaches into
# with no DMCore instance in hand (see that module's own docstring). Re-exported here unchanged
# so every existing `from DM_Social import ...` caller (ex: test_unit.py) keeps resolving.


class SocialMixin(DMCoreProtocol):
    """!
    @brief Attitudes and character/flavor-text description (DMCore mixin -- only ever composed
        into DMCore, never instantiated on its own; relies on self.entities/self.rules,
        set up by DMCore.__init__). Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes -- see DM_Types.py.
    """

    def get_attitude(self, entity_name, toward_name):
        """!
        @brief Resolves entity_name's three-value attitude array toward toward_name: a specific
            name override, then a supertype override, then the entity's default -- plus
            whatever runtime drift has accumulated toward toward_name specifically, from two
            independent sources added elementwise on top: nudge_attitude's own dialogue-tone
            drift (entity["attitude_deltas"]) and nudge_attitude_from_event's own resolved-
            action drift (entity["action_attitude_deltas"]) -- tracked, and capped, separately
            (see TALK_ATTITUDE_DRIFT_CAP/ACTION_ATTITUDE_DRIFT_CAP) since a single combined
            accumulator couldn't enforce two different ceilings on the same axis. The static
            override/default array itself is never mutated -- for a hand-authored entity it's
            the literal TOML-sourced list object, shared across every call, so this always
            returns a fresh list rather than editing that one in place.
        @param entity_name The name of the entity whose attitude is being read.
        @param toward_name The name of the entity being regarded.
        @return The [disposition, threat, familiarity] attitude array.
        """
        return Social_Resolution.get_attitude(self.entities, entity_name, toward_name)

    def _apply_capped_drift(self, entity_name, toward_name, deltas_key, axis_deltas, cap):
        """!
        @brief Shared accumulate-and-clamp primitive behind both nudge_attitude and
            nudge_attitude_from_event -- adds axis_deltas elementwise onto
            entity[deltas_key][toward_name] (creating either as needed), clamping each axis
            independently to +/-cap. A no-op for a missing entity; an all-zero axis_deltas
            entry is skipped per-axis rather than writing a no-op 0 (keeps a freshly-created
            accumulator from picking up spurious zero-valued axes it was never actually
            nudged on).
        @param entity_name The entity whose attitude is drifting.
        @param toward_name The entity it's drifting toward.
        @param deltas_key "attitude_deltas" (talk) or "action_attitude_deltas" (action).
        @param axis_deltas A three-value list, ordered like ATTITUDE_AXES, to add elementwise.
        @param cap The max |value| this accumulator may hold on any one axis.
        """
        Social_Resolution.apply_capped_drift(self.entities, entity_name, toward_name, deltas_key, axis_deltas, cap)

    def nudge_attitude(self, entity_name, toward_name, sentiments):
        """!
        @brief Applies a small, capped, persistent drift to entity_name's own attitude toward
            toward_name, driven by the tone of something toward_name just said to it -- called
            from DM_Dialogue.py's _resolve_dialogue, right before persona/attitude are read, so
            this turn's own reply already reflects the drift. Moves all three axes at once
            (disposition/threat/familiarity), each independently classified and independently
            scored -- see NLP_Core.py's classify_sentiment/classify_threat/classify_familiarity,
            all backed by the same NLI zero-shot classifier. Each axis's own magnitude is its own
            score (already 0..1) times SENTIMENT_INTENSITY_SCALE, not a flat per-sentiment
            amount -- a line the classifier read as more intensely negative/positive moves that
            axis further than a mild one.
        @param entity_name The entity whose attitude is drifting.
        @param toward_name The entity it's drifting toward (ex: self.player_name).
        @param sentiments {axis_name: (label, score)} for whichever of ATTITUDE_AXES were
            classified this turn -- an axis missing from the dict, or with a falsy label/score
            (None/unrecognized label, or a zero/None score, ex: classify_sentiment's own "no
            strong tone either way" case), contributes 0 for that axis rather than being
            treated as an error.
        """
        axis_deltas = []
        for axis in ATTITUDE_AXES:
            sentiment, score = sentiments.get(axis, (None, None))
            if sentiment not in ("negative", "positive") or not score:
                axis_deltas.append(0)
                continue
            axis_deltas.append((score if sentiment == "positive" else -score) * SENTIMENT_INTENSITY_SCALE)
        self._apply_capped_drift(entity_name, toward_name, "attitude_deltas", axis_deltas, TALK_ATTITUDE_DRIFT_CAP)

    def nudge_attitude_from_event(self, entity_name, toward_name, event_name, magnitude):
        """!
        @brief Applies a small, capped, persistent drift to entity_name's own three-axis
            attitude toward toward_name, driven by a resolved action rather than dialogue tone
            -- ex: DM_Core.py's _apply_damage_if_hit nudging a defender's disposition/threat
            down after a landed hit, or DM_Inventory.py's _resolve_transfer_intent nudging a
            theft victim's familiarity down / a gift recipient's familiarity up. Looks up
            event_name's own full-strength per-axis deltas from rules.toml's
            [[attitude_event]] table, scaling every axis by magnitude (0..1) -- the same
            "a 0..1 confidence/severity signal scales a delta" shape nudge_attitude already
            uses for dialogue sentiment, just fed a different signal (ex: net_damage / max_hp,
            or an item's value against a reference scale) and, unlike nudge_attitude, moving
            more than one axis at once. Written into its own "action_attitude_deltas"
            accumulator -- capped independently of nudge_attitude's own "attitude_deltas" (see
            ACTION_ATTITUDE_DRIFT_CAP) -- get_attitude sums both on top of the static base.
            A no-op for a missing entity/event/magnitude, an entity with no
            [entity.attitudes] table at all (the same "nothing to nudge" precedent is_hostile
            already sets for a tableless creature -- see CLAUDE.md's "Combat"), an inanimate
            object (supertype == "object"), or an entity with no HP left -- a dead (or
            never-instanced) entity isn't aware of anything happening to it or around it
            anymore, whether that's a killing blow landing, a theft, a gift, or a nearby
            battlefield bond forming (see DM_Core.py's own "shared_enemy" loop).
        @param entity_name The entity whose attitude is drifting.
        @param toward_name The entity it's drifting toward (ex: self.player_name).
        @param event_name An [[attitude_event]] entry's own "name" (ex: "combat_hit", "theft",
            "favor", "shared_enemy").
        @param magnitude How strongly this particular occurrence counts, 0..1 -- scales every
            axis delta the matched event declares.
        """
        Social_Resolution.nudge_attitude_from_event(self.entities, self.rules, entity_name, toward_name, event_name, magnitude)

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
        @brief Translates entity_name's three-value attitude array toward toward_name (from
            get_attitude) into prose via [[attitude_tier]] -- one phrase per axis, banded by
            get_attitude_tier, rather than handing the LLM raw numbers it has no way to
            calibrate ("38 disposition" means nothing to a language model; "is warm and
            well-disposed toward them" does).
        @param entity_name The name of the entity whose attitude is being described.
        @param toward_name The name of the entity it's directed toward.
        @return A prose fragment ("Attitude toward X: ..."), or "" if no attitude_tier data is
                loaded (ex: a malformed rules.toml -- see load_rules' per-file try/except note).
        """
        values = self.get_attitude(entity_name, toward_name)
        phrases = []
        for axis, value in zip(ATTITUDE_AXES, values):
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
            mechanical data (skills/dice), since this is meant to tell the LLM who someone is --
            plus, if one is currently planted (entity's own "prompt_directive",
            Social_Resolution.py's set_prompt_directive), what they're currently privately
            convinced they should do, the one piece of genuinely dynamic runtime state this
            method surfaces rather than author-set TOML.
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

        # A directive planted by Social_Resolution.py's set_prompt_directive (ex: spells.toml's
        # "suggestion" landing) -- appended right before attitude, both being the most
        # immediately action-relevant context the LLM needs to weigh before speaking/acting as
        # this entity. No expiry: there's no time-of-day/turn clock yet to hang a duration off of
        # (see CLAUDE.md's "Downtime"), so this persists until a later successful directive
        # overwrites it (or ADaM's own ad hoc entity-edit path clears it manually).
        directive = entity.get("prompt_directive")
        if directive:
            parts.append(
                f"Currently privately convinced (planted by {directive.get('source') or 'someone'}): "
                f"\"{directive.get('text')}\""
            )

        if toward_name and toward_name != entity_name:
            attitude = self.describe_attitude(entity_name, toward_name)
            if attitude:
                parts.append(attitude)

        if not parts:
            return ""
        return f"{entity.get('name', entity_name)} - " + " | ".join(parts)
