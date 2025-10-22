'''
main():
    loop:
        for each in initiative:
            if player:
                wait for player_input:
                    semantic_similarity(player_input) -> get_action, get_language
                    Named_Entity_Recognition(player_input) -> get_target
                    process_interaction(get_action, get_target)
                    process_attitudes(get_action, get_target, get_language)
            else:
                prompt LLM for npc action:
                    semantic_similarity(player_input) -> get_action, get_language
                    Named_Entity_Recognition(player_input) -> get_target
                    process_interaction(get_action, get_target)
                    process_attitudes(get_action, get_target, get_language)
            if action: rather than simple dialogue exchange):
                prompt LLM for narrative summary

process_action(get_action, get_target):

process_attitudes(get_action, get_target, get_language):

hardcoded classes:
- entity: [ type: (list of string), subtype: (list of string), name: (string), description: (string),
            max_hp: (int), cur_hp: (int), max_mp: (int), cur_mp: (int), max_fp: (int), cur_fp: (int), exp: (int),
            weight: (int), body: (string), qualities: list of (string, any), languages: list of (string),
            attributes: list of (string, float), skills: list of (skill: base, int, specializations: list of (skill: int)),
            attitudes: (default: string, string: string), allies: list of (string: string), enemies: list of (string: string),
            target: list of (string: any), area: (size), resist: list of (string, any), range: (int), slot: (string),
            cost: (initial: any, ongoing: any), value: (int) tags: list of (string), tag_mod: list of (string, string),
            inventory: list of (string, int, bool), supernatural: list of (string), memories: list of (string), quotes: list of (string), unique ]
- qualities: [ body, eyes, gender, hair, height, skin ]
- type: [ spell, object, creature, environment ]
- supernatural subtype: [ spell, miracle, power ]
- object subtype: [ weapon, armor, consumable, currency, accessory ]
- creature subtype: [ human, not_human ]
- environment subtype: [ trap ]
- attitudes: [ disposition, trust, confidence, respect, obligation ]
- languages: [ human, not_human ]
- atlas: [ array: dimension->world->area->structure->room->tile, size ]
- body: [ humanoid: self, 10 ], [ not_humanoid: self, 10 ]
- tags: [ hidden, observed, unconcious, damaged, supressed, dead, broken, dispel, damage_hp, damage_mp, damage_fp, heal_hp, heal_mp, heal_fp, prone ]
- actions: [ attack, cast, move, speak, stand, pronate, use, memorize, craft ]
- reactions: [ AoO ]

'''