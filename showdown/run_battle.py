import importlib
import json
import asyncio
import concurrent.futures
from copy import deepcopy
import logging
from datetime import date
from datetime import datetime

import data
from data.helpers import get_standard_battle_sets
import constants
import config
from showdown.engine.evaluate import Scoring
from showdown.battle import Pokemon
from showdown.battle import LastUsedMove
from showdown.battle_modifier import async_update_battle

from showdown.websocket_client import PSWebsocketClient

logger = logging.getLogger(__name__)

# now = datetime.now()
# date_time = now.strftime("%m-%d-%Y-%H-%M-%S")
# # filename = "C:\\Users\\maanc\Documents\\universidad\si\proyecto\logs\\" + date_time + ".txt"
# # file = open(# filename, "w")

def battle_is_finished(battle_tag, msg):
    return msg.startswith(">{}".format(battle_tag)) and constants.WIN_STRING in msg and constants.CHAT_STRING not in msg


async def async_pick_move(battle):
    battle_copy = deepcopy(battle)
    if battle_copy.request_json:
        battle_copy.user.from_json(battle_copy.request_json)

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        best_move = await loop.run_in_executor(
            pool, battle_copy.find_best_move
        )
    choice = best_move[0]
    if constants.SWITCH_STRING in choice:
        battle.user.last_used_move = LastUsedMove(battle.user.active.name, "switch {}".format(choice.split()[-1]), battle.turn)
    else:
        battle.user.last_used_move = LastUsedMove(battle.user.active.name, choice.split()[2], battle.turn)
    return best_move


async def handle_team_preview(battle, ps_websocket_client):
    battle_copy = deepcopy(battle)
    battle_copy.user.active = Pokemon.get_dummy()
    battle_copy.opponent.active = Pokemon.get_dummy()

    best_move = await async_pick_move(battle_copy)
    size_of_team = len(battle.user.reserve) + 1
    team_list_indexes = list(range(1, size_of_team))
    choice_digit = int(best_move[0].split()[-1])

    team_list_indexes.remove(choice_digit)
    message = ["/team {}{}|{}".format(choice_digit, "".join(str(x) for x in team_list_indexes), battle.rqid)]
    battle.user.active = battle.user.reserve.pop(choice_digit - 1)

    await ps_websocket_client.send_message(battle.battle_tag, message)


async def get_battle_tag_and_opponent(ps_websocket_client: PSWebsocketClient):
    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split('|')
        first_msg = split_msg[0]
        if 'battle' in first_msg:
            battle_tag = first_msg.replace('>', '').strip()
            user_name = split_msg[-1].replace('☆', '').strip()
            opponent_name = split_msg[4].replace(user_name, '').replace('vs.', '').strip()
            return battle_tag, opponent_name


async def initialize_battle_with_tag(ps_websocket_client: PSWebsocketClient, set_request_json=True):
    battle_module = importlib.import_module('showdown.battle_bots.{}.main'.format(config.battle_bot_module))

    battle_tag, opponent_name = await get_battle_tag_and_opponent(ps_websocket_client)

    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split('|')
        if split_msg[1].strip() == 'request' and split_msg[2].strip():
            user_json = json.loads(split_msg[2].strip('\''))
            user_id = user_json[constants.SIDE][constants.ID]
            opponent_id = constants.ID_LOOKUP[user_id]
            battle = battle_module.BattleBot(battle_tag)
            battle.opponent.name = opponent_id
            battle.opponent.account_name = opponent_name

            if set_request_json:
                battle.request_json = user_json

            return battle, opponent_id, user_json


async def read_messages_until_first_pokemon_is_seen(ps_websocket_client, battle, opponent_id, user_json):
    # keep reading messages until the opponent's first pokemon is seen
    # this is run when starting non team-preview battles
    while True:
        msg = await ps_websocket_client.receive_message()
        if constants.START_STRING in msg:
            split_msg = msg.split(constants.START_STRING)[-1].split('\n')
            for line in split_msg:
                if opponent_id in line and constants.SWITCH_STRING in line:
                    battle.start_non_team_preview_battle(user_json, line)

                elif battle.started:
                    await async_update_battle(battle, line)

            # first move needs to be picked here
            best_move = await async_pick_move(battle)
            await ps_websocket_client.send_message(battle.battle_tag, best_move)

            return

async def start_standard_battle(ps_websocket_client: PSWebsocketClient, pokemon_battle_type):
    battle, opponent_id, user_json = await initialize_battle_with_tag(ps_websocket_client, set_request_json=False)

    battle.battle_type = constants.STANDARD_BATTLE
    battle.generation = pokemon_battle_type[:4]

    smogon_usage_data = get_standard_battle_sets(pokemon_battle_type)
    data.pokemon_sets = smogon_usage_data

    if battle.generation in constants.NO_TEAM_PREVIEW_GENS:
        await read_messages_until_first_pokemon_is_seen(ps_websocket_client, battle, opponent_id, user_json)
    else:
        msg = ''
        while constants.START_TEAM_PREVIEW not in msg:
            msg = await ps_websocket_client.receive_message()

        preview_string_lines = msg.split(constants.START_TEAM_PREVIEW)[-1].split('\n')

        opponent_pokemon = []
        for line in preview_string_lines:
            if not line:
                continue

            split_line = line.split('|')
            if split_line[1] == constants.TEAM_PREVIEW_POKE and split_line[2].strip() == opponent_id:
                opponent_pokemon.append(split_line[3])

        battle.initialize_team_preview(user_json, opponent_pokemon)
        await handle_team_preview(battle, ps_websocket_client)

    return battle


async def start_battle(ps_websocket_client, pokemon_battle_type):
    battle = await start_standard_battle(ps_websocket_client, pokemon_battle_type)

    await ps_websocket_client.send_message(battle.battle_tag, [config.greeting_message])
    await ps_websocket_client.send_message(battle.battle_tag, ['/timer on'])

    return battle


async def pokemon_battle(ps_websocket_client, pokemon_battle_type):
    battle = await start_battle(ps_websocket_client, pokemon_battle_type)
    aux_battle = deepcopy(battle)
    current_turn = 0
    while True:
        msg = await ps_websocket_client.receive_message()
        if battle_is_finished(battle.battle_tag, msg):
            winner = msg.split(constants.WIN_STRING)[-1].split('\n')[0].strip()
            logger.debug("Winner: {}".format(winner))
            await ps_websocket_client.send_message(battle.battle_tag, [config.battle_ending_message])
            await ps_websocket_client.leave_battle(battle.battle_tag, save_replay=config.save_replay)
            score = get_winner_team_score(battle)
            ganador = "winner: " + str(score)
            # file.write(ganador)
            #print("-----------Puntuacion del ganador-------------", score)
            # file.close()
            return winner
        else:
            action_required = await async_update_battle(battle, msg)
            if action_required and not battle.wait:
                best_move = await async_pick_move(battle)
                await ps_websocket_client.send_message(battle.battle_tag, best_move)

        if current_turn != battle.turn:
            current_turn = battle.turn
            stringWrite = "\nturn: "+ str(battle.turn) + "| score: " + str(battle_comparation(aux_battle, battle)) + "\n"
            # file.write(stringWrite)
            # file.write("---------------------------------------\n")
            # print("Turn score:", battle_comparation(aux_battle, battle))
            aux_battle = deepcopy(battle)

        

def get_winner_team_score(battle):
    winner_team = get_winner_team(battle)
    winner_score = winner_team.active.get_normalized_hp()
    for pkm in winner_team.reserve:
        winner_score += pkm.get_normalized_hp()
    return round(winner_score, 2)

def get_winner_team(battle):
    if battle.user.active.hp == 0:
        return battle.opponent
    else:
        return battle.user

def battle_comparation(prev_battle, current_battle):
    if(current_battle.turn != 1):
        hp_user_score = 0 # prev_battle.user.active.get_normalized_hp() - current_battle.user.active.get_normalized_hp()
        hp_opponent_score = 0 # prev_battle.opponent.active.get_normalized_hp() - current_battle.opponent.active.get_normalized_hp()

        prev_user_pokemon = deepcopy(prev_battle.user.reserve)
        prev_user_pokemon.append(prev_battle.user.active)
        
        current_user_pokemon = deepcopy(current_battle.user.reserve)
        current_user_pokemon.append(current_battle.user.active)

        
        prev_opponent_pokemon = deepcopy(prev_battle.opponent.reserve)
        prev_opponent_pokemon.append(prev_battle.opponent.active)
        
        current_opponent_pokemon = deepcopy(current_battle.opponent.reserve)
        current_opponent_pokemon.append(current_battle.opponent.active)
        
        for i in range(len(prev_user_pokemon)):
            pkm_name = prev_user_pokemon[i].name
            for j in range(len(current_user_pokemon)):
                if(current_user_pokemon[j].name == pkm_name):
                    hp_user_score +=  current_user_pokemon[j].get_normalized_hp() - prev_user_pokemon[i].get_normalized_hp()             

        for i in range(len(prev_opponent_pokemon)):
            pkm_name = prev_opponent_pokemon[i].name
            for j in range(len(current_opponent_pokemon)):
                if(current_opponent_pokemon[j].name == pkm_name):
                    hp_opponent_score += current_opponent_pokemon[j].get_normalized_hp() - prev_opponent_pokemon[i].get_normalized_hp()
        return round(hp_user_score - hp_opponent_score , 2)

