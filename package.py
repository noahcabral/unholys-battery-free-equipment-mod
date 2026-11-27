"""Build Unholy's Battery-Free Equipment Mod from locally installed files."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile


CELLS = r"batteries_(?:dead|exo)"
CELL_KEY = re.compile(rf"^\s*{CELLS}\s*(?:=|$)", re.I)
CELL_WORD = re.compile(rf"\b{CELLS}\b", re.I)
FETCH_REMAP = """local function battery_free_fetch_section(actor, key, default)
    if not actor then return default end
    local section = load_var(actor, key, default)
    if section == "batteries_dead" or section == "batteries_exo" then
        section = "prt_i_copper"
        save_var(actor, key, section)
    end
    return section
end

"""


def edit(source, original, replacement, count=1):
    if source.count(original) != count:
        raise ValueError(f"Expected {count} matches for {original[:100]!r}")
    return source.replace(original, replacement)


def replace_function(source, signature, body=""):
    start = re.search(rf"(?m)^{re.escape(signature)}[ \t]*$", source)
    if not start:
        raise ValueError(f"Missing function {signature}")
    end = re.search(r"(?m)^end[ \t]*$", source[start.end():])
    if not end:
        raise ValueError(f"Unterminated function {signature}")
    return source[:start.end()] + "\n" + body + "\nend" + source[start.end() + end.end():]


def ordinary_devices(source):
    source = replace_function(source, "local function on_item_drag_dropped(obj_b, obj_d, slot_from, slot_to)")
    source = replace_function(source, "function menu_battery(obj)")
    source = replace_function(source, "function func_battery(obj)")
    source = replace_function(
        source, "function drain_device_on_event(obj, sec, idx, custom_loss)",
        '    if obj and dev_consumption[sec or obj:section()] then return true end',
    )
    source = replace_function(
        source, "function drain_device(obj, sec, loss, loss_add)",
        '    if obj and dev_consumption[sec or obj:section()] then return true end',
    )
    source = edit(source, "dev_consumption[sec][j] = cons", "dev_consumption[sec][j] = 0")
    source = edit(source, "dev_consumption_act[sec][j] = cons", "dev_consumption_act[sec][j] = 0")
    source = edit(
        source, "dev_consumption_tot[sec] = dev_consumption_tot[sec] and (dev_consumption_tot[sec] + cons) or cons",
        "dev_consumption_tot[sec] = 0", count=2,
    )
    for signature in ("item_rack", "item_slot"):
        block = f"local function {signature}(obj)\n\tlocal sec = obj:section()\n\tif dev_consumption[sec] then"
        source = edit(source, block, block + "\n\t\tif obj:condition() ~= c_full then obj:set_condition(c_full) end")
    source = edit(
        source, "\t\t-- set up stored power for device on first update\n\t\tif dev_condition[id] then",
        "\t\tif dev_consumption[sec] then\n\t\t\tdev_condition[id] = c_full\n\t\tend\n"
        "\t\t-- set up stored power for device on first update\n\t\tif dev_condition[id] then",
    )
    return source


def thermal(source):
    source = replace_function(
        source, "function get_shader_charge(weapon, zoom_type, info)",
        """    return resolve_active_powered_sight(weapon, zoom_type, info) and 1 or -1""",
    )
    source = replace_function(
        source, "local function refresh_weapon_textures(weapon, force)",
        """    local c_weapon = weapon and weapon:cast_Weapon()
    local powered = c_weapon and resolve_active_powered_sight(weapon, c_weapon:GetZoomType())
    if powered then apply_battery_texture(weapon, powered.slot, powered.section, 1, force) end""",
    )
    source = replace_function(source, "function on_canted_installed(item, weapon)")
    source = replace_function(source, "function on_canted_removed(weapon, created, section)")
    source = edit(source, "\tinstall_patches()\n\tRegisterScriptCallback(\"ActorMenu_on_item_drag_drop\", on_item_drag_dropped)\n", "")
    source = edit(source, "\t\tinstall_patches()\n\t\tstart_drain_event()\n", "")
    return source


def wearable(source):
    # Keep the existing device registration and save API. The per-item condition
    # field in this module stores charge rather than physical wear.
    start = source.index("    local tick_ms =")
    finish = source.index("    return kernel\nend", start)
    return source[:start] + """    local powered_item
    local charge = 0.9999

    function kernel.update()
        local item = cfg.get_item()
        if not item then
            powered_item = nil
            return
        end
        if item:condition() ~= charge then item:set_condition(charge) end
        local id = item:id()
        if powered_item ~= id then
            powered_item = id
            if cfg.on_power_change then cfg.on_power_change(1, false) end
        end
    end

    function kernel.is_dead()
        return false
    end

    function kernel.get_ratio()
        return 1
    end

    function kernel.reset()
        powered_item = nil
    end

    function kernel.save()
        if cfg.get_item() then return charge end
    end

    function kernel.load(value)
        powered_item = nil
    end

""" + source[finish:]


def exoskeleton(source):
    source = replace_function(
        source, "function get_data(id)",
        """    if not exo_devices[id] then return end
    local data = dup_table(exo_devices[id])
    data.power = data.supply and data.supply.max or 100
    return data""",
    )
    source = edit(
        source, "    exo_devices[id] = data",
        "    data.power = data.supply and data.supply.max or 100\n    exo_devices[id] = data",
    )
    source = re.sub(r"(?m)^(\s*)data\.power = 0$", r"\g<1>data.power = 100", source)
    source = replace_function(source, "function menu_battery(obj)")
    source = replace_function(source, "function func_battery(obj)")
    source = replace_function(source, "function charge_exo(battery, suit, data)")
    source = edit(
        source, "        if sec_d == \"batteries_exo\" then\n            -- charge exo\n"
        "            charge_exo(obj_d, obj_o, data)\n        elseif SYS_GetParam(1, sec_d, \"is_psu\") then",
        '        if SYS_GetParam(1, sec_d, "is_psu") then',
    )
    source = replace_function(source, "function exo_charged()", "    return is_wearing_exo()")
    source = replace_function(source, "function manage_drain()")
    source = edit(
        source, "function actor_on_first_update()\n    init()",
        'function actor_on_first_update()\n    init()\n    speed.add_speed("exo_speed_998585", 1, false, true)',
    )
    return source


def exo_supplies(source):
    source, count = re.subn(r'(?m)^\s*\["batteries_exo"\] = \d+,?\n', "", source)
    if count != 6:
        raise ValueError("Expected six exoskeleton vendor battery supplies")
    source = edit(source, "itm_backpack,1,batteries_exo,1,", "itm_backpack,1,")
    begin = source.index("\t\t-- two dice rolls to spawn battery and PSU")
    finish = source.index("        local psu_roll", begin)
    source = source[:begin] + (
        "        local milpsu_chance = quality_community[npc:character_community()] and 0.1 or 0.03\n"
    ) + source[finish:]
    return source


def hideout_lights(source):
    return edit(
        source, "\tself.infinite_fuel = data and data.is_world_obj",
        """\tlocal fuel = ini_sys:r_string_ex(section, "fuel_section")
\tself.infinite_fuel = (data and data.is_world_obj) or fuel == "batteries_dead" or fuel == "batteries_exo"
\tif self.infinite_fuel then self.fuel = 1 end""",
    )


def hideout_refuel(source):
    return edit(
        source, '\tif req_fuel and (sec_b == req_fuel) then\n        if sec_b == "batteries_dead" then\n'
        '            alife_create_item("batteries_dead", db.actor, {cond=obj_d:condition()})\n'
        '            obj_d:set_condition(obj_b:condition())\n        else\n'
        '            utils_item.discharge(obj_b, 1)\n            obj_d:set_condition(1.0)\n        end',
        '\tif sec_b == "batteries_dead" or sec_b == "batteries_exo" then return end\n'
        "\tif req_fuel and (sec_b == req_fuel) then\n"
        "        utils_item.discharge(obj_b, 1)\n        obj_d:set_condition(1.0)",
    )


def fetch(source):
    source = FETCH_REMAP + source
    replacements = {
        "load_var( db.actor, p[1] )": "battery_free_fetch_section(db.actor, p[1])",
        "load_var(db.actor,p[1])": "battery_free_fetch_section(db.actor, p[1])",
        'load_var(db.actor,task_id.."_fetch","")': 'battery_free_fetch_section(db.actor,task_id.."_fetch","")',
        'load_var(actor,task_id.."_fetch")': 'battery_free_fetch_section(actor,task_id.."_fetch")',
    }
    for old, new in replacements.items():
        source = source.replace(old,new)
    return source


def supplies(key, source):
    lines = []
    crafting = re.search(r"/(?:mod_)?craft(?:_.*)?\.ltx$", key)
    item_list = "new_game_loadouts" in key or "treasure_manager" in key
    mutant_loot = "mutant_loot" in key
    for line in source.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        ending = line[len(text):]
        active, comment, notes = text.partition(";")
        if CELL_KEY.match(active):
            continue
        if mutant_loot and re.match(rf"^\s*!?{CELLS}\s*,", active, re.I):
            continue
        if crafting and "=" in active and CELL_WORD.search(active):
            active = re.sub(rf",\s*{CELLS}\s*,\s*\d+(?=\s*(?:,|$))", "", active)
        if item_list and "=" in active and CELL_WORD.search(active):
            field, delimiter, value = active.partition("=")
            remaining = [part for part in value.split(",") if not CELL_WORD.search(part)]
            active = field + delimiter + ",".join(remaining)
        lines.append(active + comment + notes + ending)
    return "".join(lines)


SCRIPT_EDITS = {
    "item_device": ordinary_devices,
    "z_3dss_thermal_battery": thermal,
    "wd_battery": wearable,
    "utlf_core": lambda data: replace_function(data, "function battery_enabled()", "    return false"),
    "d_promin_config": lambda data: replace_function(data, "function get_battery_consumption()", "    return 0"),
    "item_exo_device": exoskeleton,
    "exo_loot": exo_supplies,
    "bind_light_furniture": hideout_lights,
    "placeable_furniture": hideout_refuel,
    "tasks_fetch": fetch,
    "tb_rf_receiver_packages": lambda data: edit(data, '\t{"batteries_dead","st_batteries"},\n', ""),
}

MANUAL = """Unholy's Battery-Free Equipment Mod

Install this ZIP through Mod Organizer 2 and give it priority over the equipment,
economy and crafting mods used to build it. Standalone players can stage its
gamedata in a mod manager. Use identical builds on your Romeo co-op PCs.

Detectors, PDAs, headlamps, night vision, weapon lights, wearable devices,
3DSS thermal optics, exoskeletons and battery-powered hideout lamps operate
without consumable batteries. Exoskeleton power supply upgrades still unlock
their original special abilities. Fuel for non-battery lights is unchanged.

The two battery sections remain defined so existing saves can load. Previously
placed or owned batteries can remain in older saves. New normal loot, barter,
trader supplies, RF packages, crafting and dynamic fetch tasks no longer
supply those batteries. Existing dynamic battery fetch tasks accept the same
quantity of copper parts. Battery-themed artefacts remain unaffected.

This ZIP was generated from your locally installed files. Rebuild it when the
underlying mods change.
"""


def effective_files(roots):
    def scan(root):
        files = {}
        for folder, suffix in (("configs", ".ltx"), ("scripts", ".script")):
            for directory, subdirs, filenames in os.walk(root / folder):
                subdirs[:] = [name for name in subdirs if not name.lower().endswith((".mohidden", ".bak"))]
                for name in filenames:
                    if name.lower().endswith(suffix):
                        path = Path(directory) / name
                        files[path.relative_to(root).as_posix().lower()] = path
        return files

    files = {}
    with ThreadPoolExecutor(24) as pool:
        for layer in pool.map(scan, roots):
            files.update(layer)
    return files


def effective_profile(mo2, anomaly, vanilla, profile, excluded=()):
    lines = (mo2 / "profiles" / profile / "modlist.txt").read_text(encoding="utf-8-sig").splitlines()
    excluded = set(excluded) | {"Romeo - Battery-Free Equipment", "Unholy's Battery-Free Equipment Mod"}
    enabled = [line[1:] for line in reversed(lines)
               if line.startswith("+") and not line.endswith("_separator") and line[1:] not in excluded]
    roots = [vanilla, anomaly / "gamedata"]
    roots.extend(mo2 / "mods" / mod / "gamedata" for mod in enabled)
    roots.append(mo2 / "overwrite" / "gamedata")
    return effective_files(roots)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile-index", type=Path)
    parser.add_argument("--install", type=Path, help="Folder containing the Anomaly and GAMMA directories")
    parser.add_argument("--mo2", type=Path, help="Mod Organizer base directory containing mods and profiles")
    parser.add_argument("--profile", help="Mod Organizer profile to read")
    parser.add_argument("--anomaly", type=Path, help="Anomaly game directory containing fsgame.ltx")
    parser.add_argument("--exclude-mod", action="append", default=[], help="Additional MO2 entry to ignore")
    parser.add_argument("--vanilla", type=Path, help="Read-only extract of the base game's configs and scripts")
    args = parser.parse_args()
    if args.profile_index:
        if any((args.install, args.mo2, args.anomaly, args.profile, args.vanilla, args.exclude_mod)):
            parser.error("Use --profile-index on its own")
        effective = json.loads(args.profile_index.read_text(encoding="utf-8"))
    else:
        if not args.vanilla or not all((args.vanilla / name).is_dir() for name in ("configs", "scripts")):
            parser.error("--vanilla must point to unpacked base configs and scripts")
        if args.install and any((args.mo2, args.anomaly)):
            parser.error("Use --install or specify --anomaly and --mo2")
        anomaly = args.install / "Anomaly" if args.install else args.anomaly
        mo2 = args.install / "GAMMA" if args.install else args.mo2
        if not anomaly or not (anomaly / "fsgame.ltx").is_file():
            parser.error("Supply the Anomaly game directory containing fsgame.ltx")
        if mo2 and args.profile:
            profile = mo2 / "profiles" / args.profile / "modlist.txt"
            if not (mo2 / "mods").is_dir() or not profile.is_file():
                parser.error("Supply the Mod Organizer base directory and the exact profile name")
            effective = effective_profile(mo2, anomaly, args.vanilla, args.profile, args.exclude_mod)
        elif not mo2 and not args.profile and not args.exclude_mod:
            effective = effective_files([args.vanilla, anomaly / "gamedata"])
        else:
            parser.error("--mo2 and --profile must be supplied together")
    files = {}
    for module, transform in SCRIPT_EDITS.items():
        relative = f"scripts/{module}.script"
        if relative not in effective:
            if module in {"item_device", "tasks_fetch"}:
                parser.error(f"Required game script missing: {relative}")
            continue
        source = Path(effective[relative])
        data = source.read_text(encoding="latin-1")
        bom = "\xef\xbb\xbf" if data.startswith("\xef\xbb\xbf") else ""
        files[relative] = (bom + transform(data[len(bom):])).encode("latin-1")
        print(f"{relative} <- {source}", flush=True)

    def config_override(pair):
        relative, filename = pair
        if not relative.endswith(".ltx") or not relative.startswith(("configs/items/", "configs/scripts/", "configs/mod_")):
            return None
        source = Path(filename)
        try:
            data = source.read_text(encoding="latin-1")
        except OSError:
            return None
        changed = supplies(relative, data)
        if changed != data:
            return relative, changed.encode("latin-1")

    with ThreadPoolExecutor(24) as pool:
        for changed in pool.map(config_override, effective.items()):
            if changed:
                relative, data = changed
                files[relative] = data
    with ZipFile(args.output, "x", ZIP_DEFLATED, compresslevel=7) as archive:
        archive.writestr("README.txt", MANUAL)
        for relative, data in sorted(files.items()):
            # Preserve the actual MO2 path casing.
            original = Path(effective[relative])
            parts = original.parts
            layer = next((index for index, part in enumerate(parts) if part.casefold() == "gamedata"), None)
            stored = "/".join(parts[layer + 1:]) if layer is not None else relative
            archive.writestr("gamedata/" + stored, data)
    print(f"{len(files)} overrides -> {args.output}")


if __name__ == "__main__":
    main()
