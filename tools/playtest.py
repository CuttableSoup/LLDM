"""!
@brief Long-form playtest harness. Drives the real NLPCore/LLMCore/DMCore pipeline headless
    through the event bus (no GUI). Two modes:

    Discovery -- a second LLM chooses the player's next action from the narration alone.
        Findings are for a human to read: Logs/playtest_*.jsonl (one record per turn) beside
        Logs/session_*.log (every LLM query/response, with "PLAYTEST turn N" boundary markers).

    Replay -- --replay <file> feeds a fixed input list (a previous run's .jsonl, or a .txt with
        one input per line) with no player LLM. A discovery run's inputs become a regression
        test: rerun after a fix and the harness reports every turn whose mapped skill/intent
        changed against the baseline.

    Per turn it checks hard invariants (problems -> exit code 1) and cheap heuristic flags
    (reported, exit code 0 unless --strict). Every --save-every turns it saves, reloads and
    re-saves, diffing the two dm_state.json files.

    python tools/playtest.py --turns 60 --persona explorer --seed 1
    python tools/playtest.py --replay Logs/playtest_explorer_1_123.jsonl --seed 1
"""
import argparse
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
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Event_Bus import EventBus  # noqa: E402
from Logger import Logger  # noqa: E402
from dm.DM_Core import DMCore  # noqa: E402
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
}

PLAYER_SYSTEM = (
    "You are playing a tabletop RPG character. You see only the narration. Reply with ONE "
    "short in-character action or line of speech (under 25 words), nothing else -- no quotes "
    "around it, no commentary. Personality: {persona}"
)

# A keyword-fallback skill match below this is reported as a weak match. The fallback itself
# fires down to NLPCore.keyword_fallback_floor (0.2); this flags the shaky end of that range.
WEAK_KEYWORD_SCORE = 0.35

MAPPED_SKILL_RE = re.compile(r"Mapped input to action: (\w+) via ([\w ]+?)(?: \"[^\"]*\")? \(Score: ([\d.]+)\)")


def ask_player(api_url, model, persona, history, timeout=120):
    messages = [{"role": "system", "content": PLAYER_SYSTEM.format(persona=persona)}]
    for narration, action in history[-6:]:
        messages.append({"role": "user", "content": narration})
        if action:
            messages.append({"role": "assistant", "content": action})
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.9,
                       "max_tokens": 1024}).encode()  # reasoning models spend budget thinking first
    req = urllib.request.Request(api_url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = json.load(resp)["choices"][0]["message"]["content"]
    return text.strip().splitlines()[0].strip() if text.strip() else ""


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
        # debug=True gives the same Logs/session_<ts>.log LLDM.py writes: every log line plus
        # each full LLM query/response pair. Constructed first so nothing during boot is missed.
        self.logger = Logger(self.bus, debug=True)
        self.lock = threading.Lock()
        self.responses = []
        self.counts = {"action_resolved": 0, "action_not_understood": 0,
                       "improvisation_requested": 0, "item_interaction": 0, "dialogue": 0}
        self.log_errors = []
        self.turn_skills = []   # (skill, how, score) mapped during the current turn
        self.turn_intents = []  # item-interaction intents resolved during the current turn
        self.saved_slots = []

        self.bus.subscribe("llm_response_ready", self._on_response)
        for event, key in (("action_resolved", "action_resolved"),
                           ("action_not_understood", "action_not_understood"),
                           ("improvisation_requested", "improvisation_requested"),
                           ("dialogue_resolved", "dialogue")):
            self.bus.subscribe(event, lambda d, k=key: self._count(k))
        self.bus.subscribe("item_interaction_resolved", self._on_item_interaction)
        self.bus.subscribe("log_error", lambda m: self.log_errors.append(str(m)))
        self.bus.subscribe("log_info", self._on_info)

        self.nlp = NLPCore(self.bus)
        self.llm = LLMCore(self.bus)
        self.llm.set_setting(args.setting)
        self.dm = DMCore(self.bus, scenario_name=args.scenario, setting=args.setting)
        self.player_model = args.player_model or self.llm.model

    # --- event capture -------------------------------------------------------------------

    def _on_response(self, text):
        with self.lock:
            self.responses.append(text)

    def _count(self, key):
        with self.lock:
            self.counts[key] += 1

    def _on_item_interaction(self, data):
        self._count("item_interaction")
        intent = data.get("intent") if isinstance(data, dict) else None
        self.turn_intents.append(intent or "?")

    def _on_info(self, message):
        match = MAPPED_SKILL_RE.search(str(message))
        if match:
            self.turn_skills.append((match.group(1), match.group(2), float(match.group(3))))

    def wait_for_narration(self, before, timeout=90, quiet=2.0):
        """Wait for at least one new response, then until none arrive for `quiet` seconds."""
        deadline = time.time() + timeout
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

    def next_action(self, turn, history, replay):
        """Returns (action, baseline_record); action is "" if the player LLM gave nothing."""
        if replay is not None:
            return replay[turn - 1]
        for _attempt in range(3):
            action = ask_player(self.args.player_url, self.player_model, self.persona, history)
            if action:
                return action, None
        return "", None

    def run(self):
        args = self.args
        random.seed(args.seed)  # dice use the global `random`; the LLMs stay nondeterministic
        replay = load_replay(args.replay) if args.replay else None
        turns = len(replay) if replay is not None else args.turns
        self.persona = PERSONAS.get(args.persona, args.persona)

        os.makedirs(os.path.join(ROOT, "Logs"), exist_ok=True)
        tag = "replay" if replay is not None else args.persona
        log_path = os.path.join(ROOT, "Logs", f"playtest_{tag}_{args.seed}_{int(time.time())}.jsonl")
        print(f"Turn log:    {log_path}\nSession log: {self.logger._log_file.name}")

        intro = self.wait_for_narration(0)
        history = [("\n".join(intro), None)]
        problem_turns, flag_counts, changed, turn = 0, {}, 0, 0
        with open(log_path, "w", encoding="utf-8") as log:
            for turn in range(1, turns + 1):
                try:
                    action, baseline = self.next_action(turn, history, replay)
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
                self.turn_skills, self.turn_intents = [], []
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

                record = {"turn": turn, "input": action, "narration": narration, "seconds": elapsed,
                          "skills": skills, "intents": self.turn_intents,
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

        if not args.keep_saves:
            self.cleanup_saves()
        print(f"\n{turn} turns, {problem_turns} with problems, "
              f"{self.counts['action_not_understood']} not-understood, "
              f"{self.counts['improvisation_requested']} improvised, "
              f"{self.counts['action_resolved']} resolved, "
              f"{self.counts['item_interaction']} item interactions, "
              f"{self.counts['dialogue']} dialogue.")
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
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--replay", default=None,
                   help="Feed inputs from a previous playtest .jsonl (or a one-per-line .txt) "
                        "instead of a player LLM. Turn count comes from the file.")
    p.add_argument("--save-every", type=int, default=10, help="0 disables round-trip checks")
    p.add_argument("--keep-saves", action="store_true", help="keep Saves/playtest_* slots")
    p.add_argument("--strict", action="store_true", help="exit 1 on heuristic flags too")
    p.add_argument("--player-model", default=None,
                   help="Ollama model for the player; defaults to the narrator's. Use a different "
                        "one to avoid an echo chamber (and expect GPU contention either way).")
    p.add_argument("--player-url", default="http://127.0.0.1:11434/v1/chat/completions")
    args = p.parse_args()

    # Same bootstrap LLDM.py's main() runs, but blocking: the first narration needs a live
    # server. Only ever stops a process this call started, never a pre-existing Ollama.
    ollama_process = ensure_ollama_running(log=print)
    atexit.register(stop_ollama, ollama_process)
    sys.exit(Harness(args).run())


if __name__ == "__main__":
    main()
