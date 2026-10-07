import shutil
import unittest
from resolution.Character_Creation import (
    ability_cost,
    build_character_skills,
    get_race,
    load_character_creation_data,
    load_learnable_abilities,
    load_player_starting_exp,
    race_baseline_skills,
    spend_exp_on_abilities,
    spend_exp_on_skills,
    spend_pip,
    validate_allocation,
)
from dm.DM_Core import DMCore
from tests.event_contract import ValidatingEventBus
from tests.support import (
    DMTestCase,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestCharacterCreation(unittest.TestCase):
    """!
    @brief Character_Creation.py's pure race/point-buy logic -- no DMCore, no GUI, just the
        data + math (see Character_Creation.py's own module docstring for why it's
        independent of DMCore in the first place).
    """

    @classmethod
    def setUpClass(cls):
        cls.skills, cls.races, cls.character_creation = load_character_creation_data()


    def test_human_is_defined_as_2d_in_every_skill(self):
        # No implicit "base_dice" fallback -- human's own [race.skill_dice] table explicitly
        # lists every skill at 2D, same as any other race would list its own values.
        human = get_race(self.races, "human")
        self.assertEqual(set(human["skill_dice"].keys()), set(self.skills.keys()))
        baseline = race_baseline_skills(self.skills, human)
        self.assertTrue(all(dice == 2 for dice in baseline.values()))


    def test_validate_allocation_rejects_over_the_per_skill_cap(self):
        allocation = {"arcane": 6, "stealth": 9}
        ok, reason = validate_allocation(self.skills, None, self.character_creation, allocation)
        self.assertFalse(ok)
        self.assertIn("arcane", reason)


    def test_build_character_skills_adds_allocation_onto_baseline(self):
        allocation = {"arcane": 5, "stealth": 5, "observation": 5}
        skills = build_character_skills(self.skills, get_race(self.races, "elf"), allocation)
        self.assertEqual(skills["arcane"], {"dice": 8, "pips": 0})  # 3 baseline + 5 allocated
        self.assertEqual(skills["strength"], {"dice": 1, "pips": 0})  # untouched, elf's own override
        self.assertEqual(skills["blades"], {"dice": 2, "pips": 0})  # untouched, elf's own override


    def test_load_player_starting_exp_reads_gladstones_own_authored_exp(self):
        self.assertEqual(load_player_starting_exp(), 10)  # characters.toml's own gladstone


class TestSpendPip(unittest.TestCase):
    """!
    @brief spend_pip/spend_exp_on_skills (Character_Creation.py) -- the training math a skill's
        own {dice, pips} is raised through by spending XP, one pip at a time.
    """

    def test_raising_a_pip_costs_the_current_dice_count(self):
        self.assertEqual(spend_pip(dice=3, pips=0, exp=10), (3, 1, 7))

    def test_a_third_pip_rolls_over_into_an_additional_die(self):
        # Mirrors skill_rating's own "3 pips = 1 die" scale (Challenge_Rating.py) exactly.
        self.assertEqual(spend_pip(dice=3, pips=2, exp=10), (4, 0, 7))

    def test_insufficient_exp_returns_none_and_changes_nothing(self):
        self.assertIsNone(spend_pip(dice=5, pips=0, exp=4))

    def test_spend_exp_on_skills_applies_each_entry_in_order_at_its_own_live_cost(self):
        skills = {"blades": {"dice": 2, "pips": 2}, "dodge": {"dice": 3, "pips": 0}}
        # blades: 2D2p -costs 2-> 3D0p (rolled over) -costs 3-> 3D1p; dodge: 3D0p -costs 3-> 3D1p.
        # Total spent: 2 + 3 + 3 = 8, starting from 20 XP.
        new_skills, remaining, reason = spend_exp_on_skills(
            skills, 20, ["blades", "blades", "dodge"],
        )
        self.assertIsNone(reason)
        self.assertEqual(new_skills["blades"], {"dice": 3, "pips": 1})
        self.assertEqual(new_skills["dodge"], {"dice": 3, "pips": 1})
        self.assertEqual(remaining, 12)
        # The original dict is never mutated -- a fresh copy is returned instead.
        self.assertEqual(skills["blades"], {"dice": 2, "pips": 2})

    def test_spend_exp_on_skills_is_all_or_nothing_on_insufficient_exp(self):
        skills = {"blades": {"dice": 5, "pips": 0}}
        new_skills, remaining, reason = spend_exp_on_skills(skills, 5, ["blades", "blades"])
        self.assertIsNotNone(reason)
        self.assertIn("blades", reason)
        # Nothing applied at all -- not even the first, affordable purchase.
        self.assertEqual(new_skills, skills)
        self.assertEqual(remaining, 5)

    def test_spend_exp_on_skills_rejects_an_unknown_skill_name(self):
        new_skills, remaining, reason = spend_exp_on_skills({"blades": {"dice": 2, "pips": 0}}, 10, ["nonexistent"])
        self.assertIn("nonexistent", reason)
        self.assertEqual(remaining, 10)


class TestCharacterCreationDMCoreIntegration(DMTestCase):

    def test_valid_character_creation_replaces_the_players_own_skills(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        self.assertEqual(dm.entities["gladstone"]["skills"]["strength"], {"dice": 1, "pips": 0})
        # A skill the character sheet never touches still exists, at the elf's own baseline --
        # not still carrying gladstone's own hand-authored value from characters.toml.
        self.assertEqual(dm.entities["gladstone"]["skills"]["blades"], {"dice": 2, "pips": 0})
        self.assertEqual(dm.entities["gladstone"]["qualities"]["race"], "elf")

    def test_elf_character_gains_elvish_alongside_the_templates_own_common(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        self.assertEqual(dm.entities["gladstone"]["languages"], ["common", "elvish"])

    def test_human_character_gains_no_new_language_since_common_is_already_there(self):
        character = {
            "race": "human",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        self.assertEqual(dm.entities["gladstone"]["languages"], ["common"])

    def test_pip_spend_trains_a_skill_further_using_the_players_own_starting_exp(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "pip_spend": ["arcane"],
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        # arcane: 3 baseline + 5 allocated = 8D -- one more pip costs 8 XP, gladstone starts
        # at exp = 10 (characters.toml).
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 1})
        self.assertEqual(dm.entities["gladstone"]["exp"], 10 - 8)

    def test_pip_spend_works_with_no_allocation_at_all(self):
        # "allocation" absent entirely -- pip_spend still trains gladstone's own hand-authored
        # skills directly (characters.toml's own blades = 5D).
        character = {"pip_spend": ["blades"]}
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        self.assertEqual(dm.entities["gladstone"]["skills"]["blades"], {"dice": 5, "pips": 1})
        self.assertEqual(dm.entities["gladstone"]["exp"], 10 - 5)

    def test_pip_spend_rejected_on_insufficient_exp_leaves_skills_and_exp_untouched(self):
        # Far more pips than gladstone's own 10 starting exp can ever cover.
        character = {"pip_spend": ["blades"] * 30}
        event_bus = ValidatingEventBus()
        errors = []
        event_bus.subscribe("log_error", errors.append)

        dm = DMCore(event_bus, scenario_name="debug", start_location="arena_grounds", character=character)

        self.assertEqual(dm.entities["gladstone"]["skills"]["blades"], {"dice": 5, "pips": 0})
        self.assertEqual(dm.entities["gladstone"]["exp"], 10)
        self.assertTrue(any("XP spend rejected" in e for e in errors))


class TestAbilityPurchase(unittest.TestCase):
    """!
    @brief Buying spells/techniques at character creation out of the player's starting exp
        (Character_Creation.py's ability_cost/spend_exp_on_abilities, DM_CharacterCreation.py's
        "abilities" key) -- priced difficulty / ability_cost_divisor, minimum 1.
    """

    CC = {"ability_cost_divisor": 10}
    ELF = {"race": "elf", "allocation": {"arcane": 5, "stealth": 5, "observation": 5}}

    def _boot(self, character, event_bus=None):
        return DMCore(
            event_bus or ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds",
            character=character,
        )

    def test_ability_cost_is_difficulty_over_divisor_rounded_with_a_minimum_of_one(self):
        for difficulty, cost in ((0, 1), (5, 1), (10, 1), (15, 2), (24, 2), (25, 3), (33, 3), (79, 8)):
            self.assertEqual(ability_cost({"difficulty": difficulty}, self.CC), cost, difficulty)
        self.assertEqual(ability_cost({}, self.CC), 1)
        self.assertEqual(ability_cost({"difficulty": 10}, {"ability_cost_divisor": 5}), 2)

    def test_load_learnable_abilities_finds_spells_and_techniques_only(self):
        catalog = load_learnable_abilities("Rules/Pathfinder")
        self.assertEqual(catalog["cleave"]["supertype"], "technique")
        self.assertEqual(catalog["fireball"]["supertype"], "spell")
        self.assertNotIn("gladstone", catalog)
        self.assertNotIn("flame wall", catalog)  # supertype = "object" -- a conjured effect

    def test_spend_exp_on_abilities_rejects_unknown_duplicate_and_unaffordable_all_or_nothing(self):
        catalog = {"a": {"difficulty": 10}, "b": {"difficulty": 30}}
        self.assertEqual(spend_exp_on_abilities(5, ["a", "b"], catalog, self.CC), (1, None))
        self.assertIn("Unknown", spend_exp_on_abilities(5, ["a", "nope"], catalog, self.CC)[1])
        self.assertIn("twice", spend_exp_on_abilities(5, ["a", "a"], catalog, self.CC)[1])
        remaining, reason = spend_exp_on_abilities(3, ["a", "b"], catalog, self.CC)
        self.assertEqual(remaining, 3)  # "a" was affordable but the whole purchase is rejected
        self.assertIn("Not enough XP", reason)

    def test_a_from_scratch_character_starts_with_no_abilities(self):
        dm = self._boot(self.ELF)
        self.assertEqual(dm.entities["gladstone"]["abilities"], [])
        self.assertEqual(dm.entities["gladstone"]["exp"], 10)

    def test_chosen_abilities_are_bought_out_of_exp(self):
        dm = self._boot({**self.ELF, "abilities": ["fireball", "cure disease"]})
        self.assertEqual(dm.entities["gladstone"]["abilities"], ["fireball", "cure disease"])
        self.assertEqual(dm.entities["gladstone"]["exp"], 8)  # 1 xp each (difficulty 10 and 8)

    def test_training_and_abilities_share_one_balance(self):
        # arcane is 8D, so one pip costs 8; three 1-xp abilities would need 11 of the 10 xp.
        errors = []
        event_bus = ValidatingEventBus()
        event_bus.subscribe("log_error", errors.append)
        dm = self._boot(
            {**self.ELF, "pip_spend": ["arcane"], "abilities": ["fireball", "splash flow", "arc lance"]},
            event_bus,
        )
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 1})
        self.assertEqual(dm.entities["gladstone"]["abilities"], [])
        self.assertEqual(dm.entities["gladstone"]["exp"], 2)
        self.assertTrue(any("ability purchase rejected" in e for e in errors))

    def test_an_unknown_ability_is_rejected_and_costs_nothing(self):
        errors = []
        event_bus = ValidatingEventBus()
        event_bus.subscribe("log_error", errors.append)
        dm = self._boot({**self.ELF, "abilities": ["fireball", "not a spell"]}, event_bus)
        self.assertEqual(dm.entities["gladstone"]["abilities"], [])
        self.assertEqual(dm.entities["gladstone"]["exp"], 10)
        self.assertTrue(any("ability purchase rejected" in e for e in errors))

    def test_abilities_without_an_allocation_add_to_the_templates_own(self):
        dm = self._boot({"abilities": ["cure disease"]})
        abilities = dm.entities["gladstone"]["abilities"]
        self.assertIn("fireball", abilities)  # gladstone's own hand-authored list survives
        self.assertIn("cure disease", abilities)

    def test_bought_abilities_survive_save_and_load(self):
        dm = self._boot({**self.ELF, "abilities": ["fireball"], "name": "Aria"})
        slot_name = "test_bought_abilities_round_trip_slot"
        self.addCleanup(shutil.rmtree, dm._save_slot_dir(slot_name), ignore_errors=True)

        dm.save_game(slot_name)
        dm.load_game(slot_name)

        self.assertEqual(dm.entities["Aria"]["abilities"], ["fireball"])
        self.assertEqual(dm.entities["Aria"]["exp"], 9)


class TestDefaultPlayerCharacters(unittest.TestCase):
    """!
    @brief Every is_player = true template in a setting is a selectable default character
        (list_available_characters); DMCore boots as the chosen one ("template") and drops the
        other candidates, and a save round-trips which one it was.
    """

    def test_every_setting_offers_several_defaults_gladstone_or_first_authored_first(self):
        from dm.DM_Rules import list_available_characters
        self.assertEqual(
            [n for n, _d in list_available_characters("Fantasy")],
            ["gladstone", "vesper", "brother aldric", "iona"],
        )
        self.assertEqual([n for n, _d in list_available_characters("Zombie")], ["riley", "dana", "cole"])
        self.assertGreaterEqual(len(list_available_characters("Pathfinder")), 3)
        self.assertEqual(list_available_characters("NoSuchSetting"), [])

    def test_template_picks_that_character_and_drops_the_other_candidates(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds",
                    character={"template": "iona"})
        self.assertEqual(dm.player_name, "iona")
        self.assertEqual(dm.entities["iona"]["skills"]["arcane"], {"dice": 7, "pips": 0})
        self.assertIn("iona", dm.scenario_entities)
        for other in ("gladstone", "vesper", "brother aldric"):
            self.assertNotIn(other, dm.entities)

    def test_no_template_keeps_the_first_authored_player(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")
        self.assertEqual(dm.player_name, "gladstone")
        self.assertNotIn("iona", dm.entities)

    def test_zombie_default_boots(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="rooftop", setting="Zombie", character={"template": "cole"})
        self.assertEqual(dm.player_name, "cole")

    def test_chosen_default_survives_save_and_load(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds",
                    character={"template": "vesper", "name": "Wren"})
        slot_name = "test_default_character_round_trip_slot"
        self.addCleanup(shutil.rmtree, dm._save_slot_dir(slot_name), ignore_errors=True)
        dm.save_game(slot_name)
        dm.load_game(slot_name)
        self.assertEqual(dm.player_name, "Wren")
        self.assertEqual(dm.player_template, "vesper")
        self.assertNotIn("vesper", dm.entities)
        self.assertNotIn("gladstone", dm.entities)
        self.assertEqual(dm.entities["Wren"]["skills"]["stealth"], {"dice": 5, "pips": 0})


class TestCharacterCreationRename(unittest.TestCase):
    """!
    @brief apply_character_creation's own optional "name" override (Character_Creation_GUI.py's
        name field) plus the generic "player" scenario placeholder (DM_Rules.py's
        PLAYER_PLACEHOLDER/_instance_entities) that lets a renamed character actually appear
        in a scenario without the scenario itself needing to know that name. Uses
        Rules/Fantasy/scenarios/debug.toml's own "arena_grounds"/"crypt" areas -- their own
        scenario-local "wolf"/"anne" give this exactly the rename-collision/attitude-rekey
        targets it needs.
    """


    def test_named_character_is_renamed_and_resolved_by_the_player_placeholder(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "name": "Aria",
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)

        self.assertEqual(dm.player_name, "Aria")
        self.assertNotIn("gladstone", dm.entities)  # re-keyed away, not left behind
        self.assertEqual(dm.entities["Aria"]["name"], "Aria")
        self.assertEqual(dm.entities["Aria"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        # The scenario's own "player" placeholder followed the rename into the live instance.
        self.assertIn("Aria", dm.scenario_entities)
        self.assertNotIn("gladstone", dm.scenario_entities)
        self.assertNotIn("player", dm.scenario_entities)


    def test_renaming_to_an_existing_entitys_name_is_rejected_but_skills_still_apply(self):
        errors = []
        bus = ValidatingEventBus()
        bus.subscribe("log_error", errors.append)
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            # A load_rules-level collision (creatures.toml's own "fire elemental") --
            # test_renaming_to_a_scenario_local_entitys_name_is_also_rejected below covers the
            # scenario-local case (debug.toml's own "wolf").
            "name": "fire elemental",
        }

        dm = DMCore(bus, scenario_name="debug", start_location="arena_grounds", character=character)

        self.assertEqual(dm.player_name, "gladstone")  # rename rejected
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        # untouched, not clobbered
        self.assertEqual(dm.entities["fire elemental"]["supertype"], "creature")
        self.assertTrue(any("rename rejected" in message for message in errors))

    def test_renaming_to_a_scenario_local_entitys_name_is_also_rejected(self):
        # apply_character_creation now runs after load_scenario_definition specifically so
        # this collision (against debug.toml's own local "wolf", not anything in the
        # shared Rules/Fantasy/*.toml catalog) is caught too -- previously it wasn't, since
        # the scenario's own entities hadn't been loaded into self.entities yet at the point
        # the rename's collision check ran.
        errors = []
        bus = ValidatingEventBus()
        bus.subscribe("log_error", errors.append)
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "name": "wolf",
        }

        dm = DMCore(bus, scenario_name="debug", start_location="arena_grounds", character=character)

        self.assertEqual(dm.player_name, "gladstone")  # rename rejected
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        self.assertEqual(dm.entities["wolf"]["supertype"], "creature")  # untouched, not clobbered
        self.assertTrue(any("rename rejected" in message for message in errors))

    def test_renaming_rekeys_another_entitys_attitude_override_to_the_new_name(self):
        # debug.toml's own "anne" authors [[entity.attitudes.name]] gladstone = [100, 100, 100]
        # -- a rename has to carry that override forward or anne's own scripted warmth toward
        # the player silently stops applying the moment they're renamed.
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "name": "Aria",
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt", character=character)

        anne_overrides = dm.entities["anne"]["attitudes"]["name"]
        self.assertNotIn({"gladstone": [100, 100, 100]}, anne_overrides)
        self.assertIn({"Aria": [100, 100, 100]}, anne_overrides)
        self.assertEqual(dm.get_attitude("anne", "Aria"), [100, 100, 100])

    def test_name_only_character_renames_without_touching_skills(self):
        # LLDM.py's CLI quick-boot path (a scenario + a bare character name, no interactive
        # point-buy) passes exactly this shape -- {"name": ...} with no "race"/"allocation" at
        # all -- so the skill/race override step must be skippable independently of the rename.
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character={"name": "Aria"})

        self.assertEqual(dm.player_name, "Aria")
        self.assertNotIn("gladstone", dm.entities)
        # Untouched -- characters.toml's own hand-authored value, not race_baseline_skills'.
        self.assertEqual(dm.entities["Aria"]["skills"]["blades"], {"dice": 5, "pips": 0})

    def test_a_renamed_character_survives_save_and_load(self):
        # Regression: load_rules() (called from load_game) rebuilds self.entities fresh from
        # static TOML, which re-seeds the player back under their *original* template key
        # ("gladstone"), not the renamed one -- previously nothing replayed the rename
        # afterward, so _enter_location's own self.entities[self.player_name]["band"] = 1
        # raised a bare KeyError on the saved, renamed name the moment a renamed character's
        # save was ever reloaded.
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character={"name": "Aria"})
        slot_name = "test_renamed_character_round_trip_slot"
        self.addCleanup(shutil.rmtree, dm._save_slot_dir(slot_name), ignore_errors=True)

        dm.save_game(slot_name)
        dm.load_game(slot_name)  # must not raise

        self.assertEqual(dm.player_name, "Aria")
        self.assertNotIn("gladstone", dm.entities)
        self.assertEqual(dm.entities["Aria"]["name"], "Aria")
        self.assertIn("Aria", dm.scenario_entities)

    def test_a_customized_characters_build_survives_save_and_load(self):
        # Regression: load_scenario's own _instance_entities always deep-copies fresh from the
        # template's own hand-authored skills/qualities/languages, and (unlike a "generated"
        # NPC) nothing previously saved a chargen-customized player's actual build -- so a
        # reload silently reverted any point-buy allocation/race choice back to the template's
        # bare defaults, whether or not the character was also renamed.
        character = {
            "race": "elf", "allocation": {"arcane": 5, "stealth": 5, "observation": 5}, "name": "Aria",
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        slot_name = "test_customized_character_round_trip_slot"
        self.addCleanup(shutil.rmtree, dm._save_slot_dir(slot_name), ignore_errors=True)
        built_skills = dict(dm.entities["Aria"]["skills"])
        built_languages = list(dm.entities["Aria"]["languages"])

        dm.save_game(slot_name)
        dm.load_game(slot_name)

        self.assertEqual(dm.entities["Aria"]["skills"], built_skills)
        self.assertEqual(dm.entities["Aria"]["qualities"]["race"], "elf")
        self.assertEqual(dm.entities["Aria"]["languages"], built_languages)

    def test_an_uncustomized_default_character_is_unaffected(self):
        # No character= at all -- the ordinary "no chargen ran" boot path (every test/scenario
        # that doesn't pass character) must keep working exactly as before: the template's own
        # hand-authored skills round-trip unchanged, nothing new to break.
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")
        slot_name = "test_default_character_round_trip_slot"
        self.addCleanup(shutil.rmtree, dm._save_slot_dir(slot_name), ignore_errors=True)
        original_skills = dict(dm.entities["gladstone"]["skills"])

        dm.save_game(slot_name)
        dm.load_game(slot_name)

        self.assertEqual(dm.entities["gladstone"]["skills"], original_skills)


class TestZombieArchetypeCharacterCreation(unittest.TestCase):
    """!
    @brief The character-creation pipeline against a real *non-Fantasy* setting --
        Rules/Zombie/archetypes.toml's own [[race]] tables (the same generic mechanism
        races.toml uses, proving it's genuinely setting-agnostic, not just Fantasy-shaped) plus
        their own "starting_items"/"starting_equipped" fields, which no Fantasy race authors.
    """

    def test_load_character_creation_data_finds_the_zombie_archetypes(self):
        skills, races, character_creation = load_character_creation_data("Rules/Zombie")
        self.assertEqual(
            sorted(race["name"] for race in races), ["Ex-Military", "Medic", "Scavenger"],
        )
        # Same shared point-buy constants convention as Fantasy -- Rules/Zombie/rules.toml's
        # own [character_creation] table, not a hardcoded Fantasy-only default.
        self.assertEqual(
            character_creation,
            {"pool_dice": 15, "max_allocation_per_skill": 5, "ability_cost_divisor": 10, "language_cost": 1},
        )

    def test_every_archetype_lists_every_skill_at_a_balanced_baseline(self):
        skills, races, _character_creation = load_character_creation_data("Rules/Zombie")
        for race in races:
            with self.subTest(race=race["name"]):
                self.assertEqual(set(race["skill_dice"]), set(skills))
                # 16 skills * 2D baseline, +1D on four/-1D on four cancels out -- the same
                # "no archetype starts with more or fewer total dice than any other" balance
                # races.toml's own fantasy races already follow.
                self.assertEqual(sum(race["skill_dice"].values()), 32)

    def test_archetype_chargen_replaces_the_zombie_players_own_starting_gear(self):
        character = {
            "race": "Ex-Military",
            "allocation": {"firearms": 5, "athletics": 5, "fortitude": 5},
        }
        dm = DMCore(ValidatingEventBus(), scenario_name="rooftop", setting="Zombie", character=character)

        player = dm.entities[dm.player_name]
        # Replaced outright, not appended onto riley's own hand-authored characters.toml
        # inventory (pistol/crowbar/first aid kit/pain pills).
        self.assertEqual(player["inventory"], ["combat rifle", "crowbar"])
        self.assertEqual(player["equipped"], {"primary": "combat rifle", "melee": "crowbar"})
        self.assertEqual(player["skills"]["firearms"], {"dice": 8, "pips": 0})  # 3D + 5D

    def test_archetype_chargen_boots_with_zero_validation_errors(self):
        # Belt-and-suspenders against Data_Validation.py flagging the new starting_items/
        # starting_equipped item names as unresolvable, or any other referential-integrity
        # regression from this real, non-Fantasy chargen path.
        errors = []
        bus = ValidatingEventBus()
        bus.subscribe("log_error", errors.append)
        character = {"race": "Medic", "allocation": {"medicine": 5, "charisma": 5, "observation": 5}}
        DMCore(bus, scenario_name="rooftop", setting="Zombie", character=character)
        self.assertEqual(errors, [])

    def test_a_fantasy_race_never_touches_inventory_at_all(self):
        # No Rules/Fantasy/races.toml race authors "starting_items" -- confirms the new
        # field is purely additive and doesn't change existing Fantasy chargen behavior.
        character = {"race": "elf", "allocation": {"arcane": 5, "stealth": 5, "observation": 5}}
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", character=character)
        # characters.toml's own hand-authored gladstone starting gear, untouched.
        self.assertIn("longsword", dm.entities["gladstone"]["inventory"])


if __name__ == "__main__":
    unittest.main()
