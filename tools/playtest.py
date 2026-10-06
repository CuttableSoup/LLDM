"""!
@brief Long-form playtest harness. Drives the real NLPCore/LLMCore/DMCore pipeline headless
    through the event bus (no GUI). Two modes:

    Discovery -- a second LLM chooses the player's next action from the narration alone.
        Findings are for a human to read, three files per run sharing one name under Logs/:
        playtest_<persona>_<seed>_<time>.jsonl (one record per turn), .log (every log line and
        LLM query/response, with "PLAYTEST turn N" boundary markers) and .txt (the game as the
        player saw it: narration, [System] notices and their own "> " inputs, nothing else).

    Replay -- --replay <file> feeds a fixed input list (a previous run's .jsonl, or a .txt with
        one input per line) with no player LLM. A discovery run's inputs become a regression
        test: rerun after a fix and the harness reports every turn whose mapped skill/intent
        changed against the baseline.

    Per turn it checks hard invariants (problems -> exit code 1) and cheap heuristic flags
    (reported, exit code 0 unless --strict). Every --save-every turns it saves, reloads and
    re-saves, diffing the two dm_state.json files.

    Every turn the player LLM repeats itself (LOOP_SIMILARITY against its last few inputs), its
    history is cut back to the latest narration and it's told to do something different -- a
    chaos run once spent thirty turns rephrasing one argument.

    python tools/playtest.py --turns 60 --persona explorer --seed 1
    python tools/playtest.py --mix typical,talker,brawler --turns 60 --seed 1   (20 turns each)
    python tools/playtest.py --replay Logs/playtest_explorer_1_123.jsonl --seed 1
    python tools/playtest.py --replay my_session.txt   (a real player's inputs, one per line)
"""
import argparse
import textwrap
import atexit
import glob
import json
import os
import random
import re
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Event_Bus import EventBus  # noqa: E402
from Logger import Logger  # noqa: E402
from dm.DM_Core import DMCore  # noqa: E402
from llm.LLM_Backend import BACKEND_NAMES, describe, load_backend, set_backend  # noqa: E402
from llm.LLM_Client import call_chat_completion  # noqa: E402
from llm.LLM_Core import LLMCore  # noqa: E402
from llm.Ollama_Launcher import ensure_ollama_running, stop_ollama  # noqa: E402
from nlp.NLP_Core import NLPCore  # noqa: E402

PERSONAS = {
    "explorer": "Cautious and curious. Look around, talk to people, follow leads, travel to new places.",
    "brawler": "Impulsive. Pick fights, take what isn't nailed down, and escalate.",
    "talker": "Chatty. Interrogate every NPC, haggle, buy and sell, ask about rumors and lore.",
    "terse": "Types like a real player: very short commands of one to five words, no punctuation "
             "flourishes (ex: 'look', 'attack goblin', 'talk to innkeeper', 'go north').",
    "chaos": "Adversarial tester. Use odd phrasing, typos, run-ons, multi-action sentences, "
             "ambiguous targets, items you don't own, and attempts to break the rules.",
    "gooner": "Tries to sleep with the NPCs.  Treats it as a porn game.",
    "typical": "An ordinary player at a tabletop, not a writer. Short, plain inputs, usually under "
               "twelve words: what you do ('look around', 'grab the rope', 'attack the guard'), what "
               "you say to someone (in your own words, sometimes in quotes), or a quick question. "
               "Mix all three; no stage directions or narration of other people.",
}

# A player input sharing this much of its distinctive vocabulary (5+ letter words, as a share of
# the shorter line's) with one of its last LOOP_WINDOW counts as a repeat; LOOP_REPEATS repeats in
# that window and the player is nudged (see Harness.next_action). Calibrated on past runs: 0.4
# catches both chaos spirals ("structural weight", "anti-grief annex": 9 and 4 turns) and fires at
# most once per 40 turns on healthy talker/explorer/brawler runs. Plain word overlap missed them
# entirely -- rambling lines on one topic share few words overall.
LOOP_SIMILARITY = 0.4
LOOP_WINDOW = 4
LOOP_REPEATS = 2
LOOP_NUDGE = ("You've been repeating yourself. Drop that line of play entirely and do something "
              "clearly different, still in character.")

PLAYER_SYSTEM = (
    "You are playing a tabletop RPG character. You see only the narration. Reply with ONE "
    "short in-character action or line of speech (under 25 words), nothing else -- no quotes "
    "around it, no commentary. Lines starting with [System] are the game itself talking to you, "
    "out of character; if one asks a yes/no question, answer it. Personality: {persona}"
)
# Sent on top of PLAYER_SYSTEM when the latest thing the player saw ends with a [System] yes/no
# question (an unconfirmed attack: "Attack thane? (yes/no)"). Found by playtest: the brawler
# ignored all four, so a confirmed attack was never exercised.
CONFIRMATION_NUDGE = ("The game just asked you a yes/no question. Reply with only \"yes\" or \"no\", "
                      "whichever your character would choose.")
# The same for a guard's arrest demand ("Reply with one of: pay, surrender, ..."): answered in the
# character's own words, so the reply reader's model path gets exercised too.
ARREST_MARKER = "Reply with one of:"
ARREST_NUDGE = ("A guard is arresting your character and the game listed your options. Answer as "
                "your character would, in your own words -- pay, surrender, offer a bribe with an "
                "amount, bluff your way out, or resist.")

# A keyword-fallback skill match below this is reported as a weak match. The fallback itself
# fires down to NLPCore.keyword_fallback_floor (0.2); this flags the shaky end of that range.
WEAK_KEYWORD_SCORE = 0.35

# Game mechanics named in narration (heuristic_flags). Specific phrasings only -- "roll" alone is
# also a barrel rolling or a roll of cloth.
ROLL_LEAK_PATTERN = re.compile(
    r"\b(?:(?:successful|failed|good|bad|high|low|lucky|unlucky|your) (?:dice )?rolls?\b"
    r"|dice (?:roll|rolled|came)|you rolled|rolled (?:a |an )?\d+|difficulty (?:of )?\d+"
    r"|(?:skill|ability) check|the check (?:succeeds|fails))",
    re.IGNORECASE,
)

# The player-view transcript (Harness.run): the game as the player would read it, wrapped to this.
TRANSCRIPT_WIDTH = 100


def transcript_block(text):
    """One narration or notice as the transcript shows it -- each paragraph wrapped, blank-line separated."""
    paragraphs = [p.strip() for p in str(text).split("\n") if p.strip()]
    return "\n\n".join(textwrap.fill(p, TRANSCRIPT_WIDTH) for p in paragraphs)


MAPPED_SKILL_RE = re.compile(r"Mapped input to action: (\w+) via ([\w ]+?)(?: \"[^\"]*\")? \(Score: ([\d.]+)\)")


def ask_player(api_url, model, persona, history, timeout=120, nudge=None):
    """api_url/model None: the game's own LLM backend (LLM_Backend.py), key and fallbacks included."""
    system = PLAYER_SYSTEM.format(persona=persona) + (f"\n{nudge}" if nudge else "")
    # Each history entry is (narration, the action that narration answered), so the action goes
    # first -- the latest narration must be the last thing the player model reads.
    turns = []
    for narration, action in history[-6:]:
        if action:
            turns.append({"role": "assistant", "content": action})
        turns.append({"role": "user", "content": narration})
    if turns[0]["role"] == "assistant":
        turns = turns[1:]
    messages = [{"role": "system", "content": system}, *turns]
    # max_tokens: reasoning models spend budget thinking first.
    response = call_chat_completion(api_url, messages, model=model, temperature=0.9, max_tokens=1024, timeout=timeout)
    text = response["choices"][0]["message"]["content"] or ""
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _topic_words(text):
    return {word for word in re.findall(r"[a-z']+", text.lower()) if len(word) >= 5}


def is_looping(action, previous):
    """Whether action repeats LOOP_REPEATS of the last LOOP_WINDOW inputs (see LOOP_SIMILARITY)."""
    words = _topic_words(action)
    repeats = 0
    for earlier in previous[-LOOP_WINDOW:]:
        other = _topic_words(earlier)
        if words and other and len(words & other) / min(len(words), len(other)) >= LOOP_SIMILARITY:
            repeats += 1
    return repeats >= LOOP_REPEATS


def load_replay(path):
    """Returns [(input, baseline_record_or_None)] from a .jsonl run log or a one-per-line .txt."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if path.endswith(".jsonl"):
                record = json.loads(line)
                if record.get("input"):
                    entries.append((record["input"], record))
            else:
                entries.append((line, None))
    return entries


class Harness:
    def __init__(self, args):
        self.args = args
        self.bus = EventBus()
        # One base name per run for all three of its files under Logs/: <run_name>.jsonl (turn
        # records), .txt (the player's view) and .log (the session log).
        tag = "replay" if args.replay else ("mix" if args.mix else args.persona)
        self.run_name = f"playtest_{tag}_{args.seed}_{int(time.time())}"
        # debug=True gives the same session log LLDM.py writes: every log line plus each full
        # LLM query/response pair. Constructed first so nothing during boot is missed.
        self.logger = Logger(self.bus, debug=True, log_name=self.run_name)
        self.lock = threading.Lock()
        self.responses = []
        self.counts = {"action_resolved": 0, "action_not_understood": 0, "player_notice": 0,
                       "improvisation_requested": 0, "item_interaction": 0, "dialogue": 0,
                       "implicit_dialogue": 0, "arrest_confronted": 0, "arrest_resolved": 0}
        self.log_errors = []
        self.turn_skills = []   # (skill, how, score) mapped during the current turn
        self.turn_intents = []  # item-interaction intents resolved during the current turn
        self.turn_dialogue = []  # dialogue_detected payloads seen during the current turn
        self.saved_slots = []

        self.bus.subscribe("llm_response_ready", self._on_response)
        # An out-of-character reply (a failed attempt to rephrase, a yes/no question) ends the
        # turn like narration would, and the persona sees it, so it can rephrase or answer.
        self.bus.subscribe("player_notice", self._on_notice)
        for event, key in (("action_resolved", "action_resolved"),
                           ("action_not_understood", "action_not_understood"),
                           ("improvisation_requested", "improvisation_requested"),
                           ("dialogue_resolved", "dialogue"),
                           ("arrest_confronted", "arrest_confronted"),
                           ("arrest_resolved", "arrest_resolved")):
            self.bus.subscribe(event, lambda d, k=key: self._count(k))
        self.bus.subscribe("item_interaction_resolved", self._on_item_interaction)
        self.bus.subscribe("dialogue_detected", self._on_dialogue_detected)
        self.bus.subscribe("log_error", lambda m: self.log_errors.append(str(m)))
        self.bus.subscribe("log_info", self._on_info)

        self.nlp = NLPCore(self.bus)
        self.llm = LLMCore(self.bus)
        self.llm.set_setting(args.setting)
        self._count_llm_requests()
        self.dm = DMCore(self.bus, scenario_name=args.scenario, setting=args.setting)
        self.player_model = args.player_model

    # --- event capture -------------------------------------------------------------------

    def _on_response(self, text):
        with self.lock:
            self.responses.append(text)

    def _on_notice(self, data):
        self._count("player_notice")
        with self.lock:
            self.responses.append(f"[System] {data.get('message', '')}")

    def _count(self, key):
        with self.lock:
            self.counts[key] += 1

    def _on_item_interaction(self, data):
        self._count("item_interaction")
        intent = data.get("intent") if isinstance(data, dict) else None
        self.turn_intents.append(intent or "?")

    def _on_dialogue_detected(self, data):
        self.turn_dialogue.append(data)
        if data.get("implicit"):
            self._count("implicit_dialogue")

    def _on_info(self, message):
        match = MAPPED_SKILL_RE.search(str(message))
        if match:
            self.turn_skills.append((match.group(1), match.group(2), float(match.group(3))))

    def _count_llm_requests(self):
        """
        Counts narration requests from when LLMCore queues them (synchronously, while the input
        is being handled) to when their background fetch publishes, so wait_for_narration can
        wait for every one a turn started. Found by playtest: without it, a combat round's
        narration still being generated when the next input went in was logged against that
        next turn, so a whole fast fight's log was off by one.
        """
        self.in_flight = 0
        self.in_flight_done = threading.Condition()

        def counted_queue(queue):
            def wrapper(*args, **kwargs):
                with self.in_flight_done:
                    self.in_flight += 1
                return queue(*args, **kwargs)
            return wrapper

        # Each _queue_* method starts exactly one background _fetch_and_publish.
        for name in ("_queue_narration", "_queue_dialogue", "_queue_adam_response", "_queue_scene_query"):
            setattr(self.llm, name, counted_queue(getattr(self.llm, name)))
        fetch = self.llm._fetch_and_publish

        def counted_fetch(*args, **kwargs):
            try:
                return fetch(*args, **kwargs)
            finally:
                with self.in_flight_done:
                    self.in_flight -= 1
                    self.in_flight_done.notify_all()
        self.llm._fetch_and_publish = counted_fetch

    def wait_for_narration(self, before, timeout=90, quiet=2.0):
        """
        Wait until every narration request so far has published (see _count_llm_requests) and
        at least one new response arrived, then until none arrive for `quiet` seconds.
        """
        deadline = time.time() + timeout
        with self.in_flight_done:
            self.in_flight_done.wait_for(lambda: self.in_flight == 0, max(0.0, deadline - time.time()))
        while len(self.responses) <= before and time.time() < deadline:
            time.sleep(0.2)
        last, last_change = len(self.responses), time.time()
        while time.time() - last_change < quiet:
            time.sleep(0.2)
            if len(self.responses) != last:
                last, last_change = len(self.responses), time.time()
        return self.responses[before:]

    # --- checks --------------------------------------------------------------------------

    def check_invariants(self):
        problems = []
        for name in self.dm._all_known_instance_names():
            entity = self.dm.entities.get(name, {})
            hp = self.dm.get_current_hp(name)
            if hp is None or hp != hp:
                problems.append(f"{name}: hp is {hp!r}")
            cur = entity.get("currency", 0)
            if isinstance(cur, (int, float)) and cur < 0:
                problems.append(f"{name}: negative currency {cur}")
            max_hp = entity.get("max_hp")
            if isinstance(max_hp, (int, float)) and isinstance(hp, (int, float)) and hp > max_hp:
                problems.append(f"{name}: hp {hp} > max_hp {max_hp}")
        return problems

    def heuristic_flags(self, narration):
        """Cheap smells that aren't crashes: each one below was a real finding in a past run."""
        flags = []
        text = " ".join(narration)
        for skill in self.nlp.matcher.skills_data:
            pattern = rf"\b(?:successful\w*\s+(?:\w+\s+){{0,3}}{skill}|your\s+(?:\w+\s+)?{skill})\b"
            if re.search(pattern, text, re.IGNORECASE):
                flags.append(f"skill name leaked into narration: '{skill}'")
        # The dice themselves showing through. Found by playtest: "The successful roll means your
        # strike connects cleanly".
        roll_leak = ROLL_LEAK_PATTERN.search(text)
        if roll_leak:
            flags.append(f"dice leaked into narration: '{roll_leak.group(0)}'")
        # self.dm.entities, not _all_known_instance_names() -- the latter only covers instances
        # save_game diffs against a TOML template, which narration-driven ad hoc population
        # (DM_Improvisation.py) never registers there (see _collect_ad_hoc_entities).
        for name in self.dm.entities:
            if not name or len(name) < 3:
                continue
            if re.search(rf"\bthe {re.escape(name.title())}\b", text):
                flags.append(f"entity name used as a title: 'the {name.title()}'")
        for skill, how, score in self.turn_skills:
            if how == "keyword fallback" and score < WEAK_KEYWORD_SCORE:
                flags.append(f"weak skill match: {skill} via keyword fallback ({score:.2f})")
        # ADaM (or anyone) talking about its own prompt. Found by playtest: "There are no facts
        # provided regarding...", six times in one chaos run.
        if re.search(r"\b(?:facts|information|data) (?:provided|given)\b|\bprovided (?:facts|information|data|lore)\b",
                     text, re.IGNORECASE):
            flags.append("narration mentions its prompt ('facts provided')")
        # Dialogue-only smells: the reply should be someone talking to "you".
        if self.turn_dialogue:
            if re.search(r"\bthe (?:player|user)\b", text, re.IGNORECASE):
                flags.append("dialogue says 'the player'/'the user'")
            if not re.search(r'["\u201c\u201d]', text):
                flags.append("dialogue reply has no quoted speech")
            if "*(" in text:
                flags.append("dialogue reply has a meta parenthetical")
        return flags

    def roundtrip_check(self, turn):
        """save -> load -> save again; the two dm_state.json files must match."""
        a, b = f"playtest_{turn}_a", f"playtest_{turn}_b"
        self.saved_slots += [a, b]
        self.dm.save_game(a)
        self.dm.load_game(a)
        self.dm.save_game(b)
        sa, sb = self._slot_state(a), self._slot_state(b)
        if sa == sb:
            return []
        return [f"save/load round-trip drift in keys: "
                f"{[k for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]}"]

    def _slot_state(self, slot):
        with open(os.path.join(ROOT, "Saves", slot, "dm_state.json")) as f:
            return json.load(f)

    def cleanup_saves(self):
        for slot in self.saved_slots:
            shutil.rmtree(os.path.join(ROOT, "Saves", slot), ignore_errors=True)

    # --- main loop -----------------------------------------------------------------------

    def persona_for(self, turn):
        """The persona playing this turn -- --mix hands over every --turns/len(mix) turns."""
        if not self.mix:
            return self.args.persona
        share = max(1, -(-self.args.turns // len(self.mix)))
        return self.mix[min((turn - 1) // share, len(self.mix) - 1)]

    def next_action(self, turn, history, replay):
        """
        Returns (action, baseline_record, nudged); action is "" if the player LLM gave nothing.
        A repeat of its own recent inputs (is_looping) is asked again with the history cut back
        to the latest narration and LOOP_NUDGE added; that history cut sticks.
        """
        if replay is not None:
            return (*replay[turn - 1], False)
        persona_name = self.persona_for(turn)
        persona = PERSONAS.get(persona_name, persona_name)
        previous = [action for _narration, action in history if action]
        nudge = None
        question = history[-1][0] if history else ""
        if "(yes/no)" in question or ARREST_MARKER in question:
            answer_nudge = CONFIRMATION_NUDGE if "(yes/no)" in question else ARREST_NUDGE
            action = ask_player(self.args.player_url, self.player_model, persona, history, nudge=answer_nudge)
            if action:
                return action, None, False
        for _attempt in range(3):
            action = ask_player(self.args.player_url, self.player_model, persona, history, nudge=nudge)
            if not action:
                continue
            if nudge is None and is_looping(action, previous):
                del history[:-1]
                nudge = LOOP_NUDGE
                continue
            return action, None, nudge is not None
        return "", None, nudge is not None

    def run(self):
        args = self.args
        random.seed(args.seed)  # dice use the global `random`; the LLMs stay nondeterministic
        replay = load_replay(args.replay) if args.replay else None
        turns = len(replay) if replay is not None else args.turns
        self.mix = [name.strip() for name in args.mix.split(",") if name.strip()] if args.mix else []

        os.makedirs(os.path.join(ROOT, "Logs"), exist_ok=True)
        tag = "replay" if replay is not None else ("mix" if self.mix else args.persona)
        log_path = os.path.join(ROOT, "Logs", f"{self.run_name}.jsonl")
        # What the player would have seen: narration, [System] notices and their own inputs, nothing
        # else -- for reading a run as a game rather than as data.
        transcript_path = os.path.join(ROOT, "Logs", f"{self.run_name}.txt")
        print(f"Turn log:    {log_path}\nTranscript:  {transcript_path}\nSession log: {self.logger._log_file.name}")

        intro = self.wait_for_narration(0)
        history = [("\n".join(intro), None)]
        problem_turns, flag_counts, changed, nudges, turn = 0, {}, 0, 0, 0
        with open(log_path, "w", encoding="utf-8") as log, open(transcript_path, "w", encoding="utf-8") as transcript:
            transcript.write(f"{args.scenario} ({args.setting}) -- {tag}, seed {args.seed}, "
                             f"{time.strftime('%Y-%m-%d %H:%M')}\n{'=' * TRANSCRIPT_WIDTH}\n\n")
            transcript.write("\n\n".join(transcript_block(text) for text in intro) + "\n")
            transcript.flush()
            for turn in range(1, turns + 1):
                try:
                    action, baseline, nudged = self.next_action(turn, history, replay)
                except Exception as e:  # player LLM down: stop rather than spam empty turns
                    print(f"player LLM failed on turn {turn}: {e}")
                    break
                if not action:
                    # Logged, not silently skipped: a gap in the JSONL should always be explained.
                    log.write(json.dumps({"turn": turn, "skipped": "player LLM returned empty "
                                          "text 3 times"}) + "\n")
                    log.flush()
                    print(f"[turn {turn}] skipped: player LLM returned empty text")
                    continue

                before_counts, before_errors = dict(self.counts), len(self.log_errors)
                before = len(self.responses)
                self.turn_skills, self.turn_intents, self.turn_dialogue = [], [], []
                t0 = time.time()
                # Boundary marker so each turn's LLM exchanges can be found in the session log.
                self.bus.publish("log_info", f"PLAYTEST turn {turn} input: {action}")
                self.bus.publish("user_input_submitted", action)
                narration = self.wait_for_narration(before)
                elapsed = round(time.time() - t0, 1)

                problems = self.check_invariants()
                if args.save_every and turn % args.save_every == 0:
                    problems += self.roundtrip_check(turn)
                if not narration:
                    problems.append("no narration within timeout")
                if any("Could not connect" in n or "empty response" in n for n in narration):
                    problems.append("LLM error narration")
                # Ollama is up and answering (no connection/empty-response error) but the model
                # broke character and echoed a prompt-engineering placeholder back instead of
                # narrating -- a distinct failure mode, seen in practice (ex: "*(Please provide
                # the player's question or action...)*").
                if any(re.search(r"please provide|as the (?:game master|dm|dungeon master)[,)]|"
                                 r"i (?:need|require) (?:more|the) (?:context|information)",
                                 n, re.IGNORECASE) for n in narration):
                    problems.append("LLM broke character / echoed a meta-instruction")

                flags = self.heuristic_flags(narration)
                skills = [s for s, _how, _score in self.turn_skills]
                if baseline is not None and baseline.get("skills") is not None:
                    old = (baseline.get("skills"), baseline.get("intents", []))
                    if old != (skills, self.turn_intents):
                        changed += 1
                        flags.append(f"mapping changed: {old[0]}+{old[1]} -> {skills}+{self.turn_intents}")

                if nudged:
                    nudges += 1
                    print(f"[turn {turn}] player was looping; nudged")
                record = {"turn": turn, "input": action, "narration": narration, "seconds": elapsed,
                          "persona": None if replay is not None else self.persona_for(turn),
                          "nudged": nudged,
                          "skills": skills, "intents": self.turn_intents,
                          "speech": [{"form": d.get("speech_form"), "implicit": d.get("implicit")}
                                     for d in self.turn_dialogue],
                          "events": {k: self.counts[k] - before_counts[k] for k in self.counts},
                          "problems": problems, "flags": flags,
                          "log_errors": self.log_errors[before_errors:]}
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                log.flush()
                for flag in flags:
                    key = flag.split(":")[0]
                    flag_counts[key] = flag_counts.get(key, 0) + 1
                if problems:
                    problem_turns += 1
                    print(f"[turn {turn}] PROBLEM {action!r} -> {problems}")
                for flag in flags:
                    print(f"[turn {turn}] flag: {flag}")
                history.append(("\n".join(narration) or "(nothing happened)", action))
                transcript.write(f"\n> {action}\n\n")
                transcript.write("\n\n".join(transcript_block(text) for text in narration) or "(nothing happened)")
                transcript.write("\n")
                transcript.flush()

        if not args.keep_saves:
            self.cleanup_saves()
        print(f"\n{turn} turns, {problem_turns} with problems, "
              f"{self.counts['action_not_understood']} not-understood, "
              f"{self.counts['improvisation_requested']} improvised, "
              f"{self.counts['action_resolved']} resolved, "
              f"{self.counts['item_interaction']} item interactions, "
              f"{self.counts['dialogue']} dialogue ({self.counts['implicit_dialogue']} implicit), "
              f"{self.counts['arrest_confronted']} arrest demands, "
              f"{self.counts['arrest_resolved']} arrests resolved, "
              f"{nudges} loop nudges.")
        if flag_counts:
            print("Flags: " + ", ".join(f"{k} x{v}" for k, v in sorted(flag_counts.items())))
        if replay is not None:
            print(f"Replay: {changed} of {turns} turns changed mapping vs baseline.")
        return 1 if problem_turns or (args.strict and flag_counts) else 0


def main():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--scenario", default="lost_coast")
    p.add_argument("--setting", default="Pathfinder")
    p.add_argument("--turns", type=int, default=50)
    p.add_argument("--persona", default="explorer", help=f"one of {list(PERSONAS)} or free text")
    p.add_argument("--mix", default=None,
                   help="comma-separated personas sharing --turns in order, one game "
                        "(ex: typical,talker,brawler); overrides --persona")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--replay", default=None,
                   help="Feed inputs from a previous playtest .jsonl (or a one-per-line .txt) "
                        "instead of a player LLM. Turn count comes from the file.")
    p.add_argument("--save-every", type=int, default=10, help="0 disables round-trip checks")
    p.add_argument("--keep-saves", action="store_true", help="keep Saves/playtest_* slots")
    p.add_argument("--strict", action="store_true", help="exit 1 on heuristic flags too")
    p.add_argument("--player-model", default=None,
                   help="Model for the player; defaults to the narrator's. Use a different "
                        "one to avoid an echo chamber (and expect GPU contention either way).")
    p.add_argument("--player-url", default=None,
                   help="Endpoint for the player LLM; defaults to the narrator's own backend.")
    p.add_argument("--llm", choices=BACKEND_NAMES, default=None,
                   help="LLM backend, as LLDM.py's --llm (default: llm_config.toml, else local).")
    args = p.parse_args()

    backend = load_backend(args.llm)
    set_backend(backend)
    print(describe(backend))
    if backend.launches_ollama:
        # Same bootstrap LLDM.py's main() runs, but blocking: the first narration needs a live
        # server. Only ever stops a process this call started, never a pre-existing Ollama.
        ollama_process = ensure_ollama_running(model=backend.model, log=print)
        atexit.register(stop_ollama, ollama_process)
    sys.exit(Harness(args).run())


if __name__ == "__main__":
    main()
