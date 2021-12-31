import csv
import json
import logging
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from enum import Enum
from itertools import chain, starmap
from math import floor
from pathlib import Path
from string import ascii_lowercase
from typing import Any, Iterator, Optional, Tuple

from idleon_saver.data import (
    Bags,
    bag_maps,
    card_reqs,
    class_names,
    cog_boosts,
    cog_datas_map,
    cog_type_map,
    constellation_names,
    gamedata,
    pouch_names,
    pouch_sizes,
    skill_names,
    starsign_ids,
    starsign_names,
    statue_names,
    vial_names,
)
from idleon_saver.ldb import ldb_args
from idleon_saver.utility import from_keys_in, zip_from_iterable

logger = logging.getLogger(__name__)


class Formats(Enum):
    IC = "idleon_companion"
    COG = "cogstruction"

    @staticmethod
    def lookup(key: str):
        for member in Formats.__members__.values():
            if member.value == key:
                return member


def friendly_name(s: str) -> str:
    return s.replace("_", " ").title()


def get_baseclass(which: int) -> int:
    if which in range(1, 5 + 1):
        # special case for beginner because it has only 4 subclasses
        return 1

    for base in [7, 19, 31]:  # warrior, archer, mage
        # each has 6 subclasses (unreleased but still in ClassNames)
        if which in range(base, base + 6 + 1):
            return base

    raise ValueError(f"Class {which} does not exist")


def get_classname(which: int) -> str:
    return friendly_name(class_names[which])


def get_starsign_from_index(i: int) -> str:
    return starsign_ids[starsign_names[i]]


def get_cardtier(name: str, level: int) -> int:
    req = card_reqs[name]
    if level == 0:
        return 0
    elif level >= req * 9:
        return 4
    elif level >= req * 4:
        return 3
    elif level >= req:
        return 2
    else:
        return 1


def get_pouchsize(itemtype: str, stacksize: int) -> str:
    return (
        "Mini"
        if stacksize == 25 and itemtype == "bCraft"
        else "Miniscule"
        if stacksize == 25 and itemtype == "Foods"
        else pouch_sizes[stacksize]
    )


def get_pouches(carrycaps: dict[str, int]) -> dict[str, bool]:
    return {
        " ".join(
            [
                get_pouchsize(itemtype, stacksize),
                pouch_names[itemtype],
                "Pouch",
            ]
        ): True
        for itemtype, stacksize in carrycaps.items()
        if stacksize > 10
    }


def get_empties(cogs: list[str]) -> list[dict[str, int]]:
    if len(cogs) < 96:
        raise ValueError(
            "cog list must contain at least 96 entries to cover the whole cog board; "
            f"{len(cogs)} isn't enough"
        )

    empties: list[dict[str, int]] = []
    # The cog board is 8 rows by 12 columns = 96 spaces.
    for y in range(8):
        for x in range(12):
            i = y * 12 + x
            # Ignore occupied spaces.
            if cogs[i] == "Blank":
                empties.append({"empties_x": x, "empties_y": y})

    return empties


def get_cog_type(name: str) -> Optional[str]:
    # Check simple special cases.
    if name == "Blank":
        return None
    elif name.startswith("Player_"):
        return "Character"
    elif name == "CogY":
        return "Yang_Cog"
    elif name.startswith("CogZ"):
        return "Omni_Cog"

    # Check each type of directional cog.
    for direction, cog_type in cog_type_map.items():
        if name.endswith(direction):
            return f"{cog_type}_Cog"

    # If the name didn't match any special cases, it's just a regular cog.
    return "Cog"


def get_cog_data(cog: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    data = {}

    cog_type = get_cog_type(name)
    if cog_type is None:
        return None

    data["cog type"] = cog_type
    data["name"] = name.removeprefix("Player_") if cog_type == "Character" else ""

    for key, field in cog_datas_map.items():
        try:
            # Boosts are stored as percentages, so convert them to multipliers.
            data[field] = cog[key] / 100 if key in cog_boosts else cog[key]
        except KeyError:
            # Cogs only have keys for whatever bonuses they have,
            # but we need to fill in the missing fields for DictWriter.
            data[field] = ""

    return data


class Exporter(ABC):
    def __init__(self, savedata: dict) -> None:
        self.savedata = savedata

        self.classes: list[int] = self.all_players("CharacterClass")
        self.skill_levels: list[list[int]] = self.all_players("Lv0")
        self.statue_levels: list[list[list]] = self.all_players("StatueLevels")
        self.bags_used: list[dict[str, Any]] = self.all_players("InvBagsUsed")
        self.carrycaps = self.all_players("MaxCarryCap")

        # Abstract attributes defined by subclasses
        self.names: list[str]
        self.stats: list[list[int]]
        self.cauldron: list[list[str]]
        self.starsigns_unlocked: dict[str, int]
        self.starsigns_prog: list[tuple[str, int]]
        self.starsigns_equipped: list[str]
        self.cards: dict[str, int]
        self.stamp_levels: list[list[int]]
        self.statues_golden: list[int]
        self.cog_map: list[dict[str, Any]]
        self.cog_order: list[str]

    @abstractmethod
    def all_players(self, key: str) -> list:
        raise NotImplementedError

    def export(self, fmt: Formats, workdir: Path):
        if fmt == Formats.IC:
            self.save_idleon_companion(workdir)
        elif fmt == Formats.COG:
            self.save_cogstruction(workdir)
        else:
            raise ValueError(
                f"Format must be idleon_companion or cogstruction, not {fmt}"
            )

    def save_idleon_companion(self, workdir: Path):
        outfile = workdir / "idleon_companion.json"

        with open(outfile, "w", encoding="utf-8") as file:
            json.dump(self.to_idleon_companion(), file)

        logger.info(f"Wrote file: {outfile}")

    def to_idleon_companion(self) -> dict:
        return {
            "alchemy": self.get_alchemy(),
            "starSigns": self.get_starsigns(),
            "cards": self.get_cards(),
            "stamps": {name: level for name, level in self.get_stamps() if level > 0},
            "statues": self.get_statues(),
            "checklist": self.get_checklist(),
            "chars": self.get_chars(),
        }

    def save_cogstruction(self, workdir: Path):
        data = self.to_cogstruction()

        for which in ["cog_datas", "empties_datas"]:
            outfile = workdir / f"{which}.csv"

            with open(outfile, "w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=data[which][0].keys())

                writer.writeheader()
                for row in data[which]:
                    writer.writerow(row)

            logger.info(f"Wrote file: {outfile}")

    def to_cogstruction(self) -> dict[str, Any]:
        return {
            "cog_datas": list(
                filter(
                    None,
                    starmap(
                        get_cog_data,
                        zip(self.cog_map, self.cog_order),
                    ),
                )
            ),  # Filter to ignore blank cogs.
            "empties_datas": get_empties(self.cog_order),
        }

    def char_map(self) -> dict[str, str]:
        return dict(zip(self.names, "_" + ascii_lowercase))

    def get_alchemy(self):
        return {
            "upgrades": dict(
                zip(
                    ("Orange", "Green", "Purple", "Yellow"),
                    self.cauldron[:4],
                )
            ),
            "vials": {
                friendly_name(name): level
                for name, level in zip(vial_names, self.cauldron[4])
                if level > 0
            },
        }

    def get_starsigns(self) -> dict[str, bool]:
        return {
            starsign_ids[name]: bool(unlocked)
            for name, unlocked in self.starsigns_unlocked.items()
        }

    def get_cards(self) -> dict[str, int]:
        return {
            gamedata["monsterNames"][name]: get_cardtier(name, level)
            for name, level in self.cards.items()
            if level > 0
        }

    def get_stamps(self) -> Iterator[Tuple[str, int]]:
        return chain.from_iterable(
            zip(stamps, levels)
            for stamps, levels in zip(gamedata["stampList"].values(), self.stamp_levels)
        )

    def get_statues(self) -> dict:
        return {
            name: {
                "golden": bool(gold),
                "level": max(lvls),
                "progress": floor(max(progs)),
            }
            for name, gold, lvls, progs in zip(
                statue_names,
                self.statues_golden,
                *zip(
                    *[
                        zip(*statues)
                        for statues in zip_from_iterable(self.statue_levels)
                    ]
                ),
            )
        }

    def get_checklist(self) -> dict[str, bool]:
        return (
            from_keys_in(
                bag_maps[Bags.GEM],
                self.bags_used[0],
                True,
            )
            | from_keys_in(
                bag_maps[Bags.STORAGE], self.savedata["InvStorageUsed"].keys(), True
            )
            | {name: True for name, level in self.get_stamps() if level > 0}
        )

    def get_player_constellations(self, charname: str) -> dict[str, bool]:
        return {
            constellation_names[i]: True
            for i, (chars, completed) in enumerate(self.starsigns_prog)
            if self.char_map()[charname] in (chars or "")  # chars can be null
        }

    def parse_player_starsigns(self, starsign_codes: str):
        return {
            get_starsign_from_index(int(k)): True
            for k in starsign_codes.strip(",_").split(",")
        }

    def build_char(self, name, klass, stats, starsigns, skills, bags, carrycaps):
        return {
            "name": name,
            "class": get_classname(klass),
            "level": stats[4],
            "constellations": self.get_player_constellations(name),
            "starSigns": self.parse_player_starsigns(starsigns),
            "skills": dict(list(zip(skill_names, skills))[1:]),
            "items": from_keys_in(bag_maps[Bags.INV], bags.keys(), True)
            | get_pouches(carrycaps),
        }

    def get_chars(self) -> list[dict]:
        return list(
            starmap(
                self.build_char,
                zip(
                    self.names,
                    self.classes,
                    self.stats,
                    self.starsigns_equipped,
                    self.skill_levels,
                    self.bags_used,
                    self.carrycaps,
                ),
            )
        )


class LocalExporter(Exporter):
    def __init__(self, savedata: dict) -> None:
        super().__init__(savedata)
        self.names = savedata["GetPlayersUsernames"]
        self.stats = [pv["StatList"] for pv in self.all_players("PersonalValuesMap")]
        self.starsigns_equipped = [
            pv["StarSign"] for pv in self.all_players("PersonalValuesMap")
        ]
        self.starsigns_unlocked = savedata["StarSignsUnlocked"]
        self.starsigns_prog = savedata["StarSignProg"]
        self.cauldron = savedata["CauldronInfo"]
        self.cards = savedata["Cards"][0]
        self.stamp_levels = savedata["StampLevel"]
        self.statues_golden = savedata["StatueG"]
        self.cog_map = savedata["CogMap"]
        self.cog_order = savedata["CogOrder"]

    def all_players(self, key: str) -> list:
        return [player[key] for player in self.savedata["PlayerDATABASE"].values()]


def main(args: Namespace):
    infile = args.workdir / (args.infile or "decoded.json")
    with open(infile, encoding="utf-8") as file:
        data = json.load(file)
    LocalExporter(data).export(args.to, args.workdir)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--to",
        choices=list(Formats),
        type=Formats.lookup,
        default=Formats.IC,
        help="format to parse save data into",
    )
    main(ldb_args(parser))
