import constants
import random
# from showdown.run_battle import  file
from data import all_move_json
from showdown.battle import Battle
from showdown.engine.damage_calculator import calculate_damage
from showdown.engine.find_state_instructions import update_attacking_move
from ..helpers import format_decision


class BattleBot(Battle):
    def __init__(self, *args, **kwargs):
        super(BattleBot, self).__init__(*args, **kwargs)

    def find_best_move(self):
        state = self.create_state()
        my_options = self.get_all_options()[0]

        moves = []
        switches = []
        for option in my_options:
            if option.startswith(constants.SWITCH_STRING + " "):
                switches.append(option)
                moves.append(option)
            else:
                moves.append(option)

        if self.force_switch or not moves:
            return format_decision(self, switches[0])

        n = random.randint(0,len(moves) - 1)
        choice = moves[n]
        eleccion = ""
        if(choice.startswith(constants.SWITCH_STRING + " ")):
            eleccion = choice
        else:
            eleccion = "mov: " + choice + " | " + all_move_json[choice]["type"]
        # file.write(eleccion)

        return format_decision(self, choice)
