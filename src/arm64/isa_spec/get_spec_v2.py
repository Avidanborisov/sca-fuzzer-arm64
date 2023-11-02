#!/usr/bin/env python3
import json
import subprocess
import os
import sys
import glob
import importlib.util
import requests
import re

# URL of the AArch64MappingInsn.inc file, for implicit registers information
MAPPING_INC_URL = "https://raw.githubusercontent.com/capstone-engine/capstone/46154e8605aaefdcca5fecf4ea88b92db5a40ad3/arch/AArch64/AArch64MappingInsn.inc"
# Path to save the downloaded AArch64MappingInsn.inc file
MAPPING_INC_PATH = "AArch64MappingInsn.inc"


def parse_mapping_file(path):
    with open(path, "r") as f:
        content = f.read()

    pattern = re.compile(
        r"{\s*(AArch64_\w+),\s*(ARM64_INS_\w+).*?{([^}]*)}.*?{([^}]*)}", re.DOTALL
    )
    insn_reg_map = {}

    for match in re.finditer(pattern, content):
        _, insn_with_prefix, regs_use, regs_mod = match.groups()
        insn_name = insn_with_prefix.replace("ARM64_INS_", "")  # Remove the prefix

        # Process only up to the first 0 encountered in regs_use and regs_mod
        regs_use = [
            reg.strip().replace("ARM64_REG_", "")
            for reg in regs_use.split(",")
            if reg.strip() and reg.strip() != "0"
        ]
        regs_mod = [
            reg.strip().replace("ARM64_REG_", "")
            for reg in regs_mod.split(",")
            if reg.strip() and reg.strip() != "0"
        ]

        insn_reg_map[insn_name] = {"regs_use": regs_use, "regs_mod": regs_mod}

    return insn_reg_map


def get_implicit_operands(insn, implicit_mapping):
    info = implicit_mapping.get(insn["name"])
    if info is None:
        return []

    implicit_operands = []

    for reg in info["regs_use"]:
        is_read = True
        is_write = reg in info["regs_mod"]

        if reg == "NZCV":
            width = 0
            type_ = "FLAGS"
            p = "r/w" if (is_read and is_write) else "w" if is_write else "r"
            values = [p, "", "", p, p, "", "", "", p]
        else:
            width = 64
            type_ = "REG"
            values = [reg]

        implicit_operands.append(
            {
                "dest": is_write,
                "src": is_read,
                "type_": type_,
                "width": width,
                "values": values,
            }
        )

    if insn["control_flow"]:
        implicit_operands.append(
            {
                "dest": False,
                "src": True,
                "type_": "REG",
                "width": 64,
                "values": ["PC"],
            }
        )

    return implicit_operands


def get_operands_list(insn):
    operands_list = []
    index = 0
    while True:
        try:
            op = insn["operands"][index]
            op_str = str(op).replace("AARCH64_OPND_", "")
            if op_str == "NIL":
                break  # Stop if we hit the NIL operand
            operands_list.append(op_str)
            if op_str.startswith("ADDR_UIMM") or op_str.startswith("ADDR_SIMM"):
                operands_list.append(op_str.replace("ADDR_", ""))
            index += 1
        except (gdb.error, IndexError):
            # If an IndexError is raised, we've reached the end of the operands.
            break
    return operands_list


def process_operand(insn, operand: str):
    supported_immediate_types = {
        "AIMM": ["[0-4095]"],
        "LIMM": ["bitmask"],
        "IMMR": ["[0-63]"],
        "IMMS": ["[0-63]"],
        "CCMP_IMM": ["[0-31]"],
        "NZCV": ["[0-15]"],
        "UIMM4": ["[0-15]"],
        "UIMM7": ["[0-127]"],
        "HALF": ["[0-65535]"],
        "BIT_NUM": ["[0-63]"],
        "SIMM9": ["[-256-255]"],
    }

    if operand.startswith("R") and operand != "RPRFMOP":
        type_ = "REG"
        width = 64
        is_dest = "Rd" in operand or (operand == "Rt" and insn["name"].startswith("LDR"))
        values = ["GPR"]
        if "SP" in operand:
            values.append("SP")
    elif operand.startswith("ADDR_PCREL") or operand.startswith("ADDR_ADRP"):
        type_ = "LABEL"
        width = 0
        is_dest = False
        values = []
    elif operand.startswith("ADDR_UIMM") or operand.startswith("ADDR_SIMM"):
        type_ = "MEM"
        width = 64
        is_dest = insn["name"].startswith("STR")
        values = []
    elif operand.startswith("COND"):
        type_ = "FLAGS"
        width = 0
        is_dest = False
        values = []
    elif operand in supported_immediate_types:
        type_ = "IMM"
        width = 64
        is_dest = False
        values = supported_immediate_types[operand]
    else:
        return None


    return {
        "dest": is_dest,
        "src": not is_dest,
        "comment": operand,
        "type_": type_,
        "width": width,
        "values": values,
    }


def get_operands(insn, raw_insn):
    operands = []

    for operand in get_operands_list(raw_insn):
        operand_info = process_operand(insn, operand)
        if operand_info is None: # ignore instructions with unsupported operands
            return None

        operands.append(operand_info)

    return operands

def get_aarch64_opcode_table_json():
    # Access the aarch64_opcode_table
    table = gdb.parse_and_eval("aarch64_opcode_table")
    table_length = int(
        gdb.parse_and_eval(
            "sizeof(aarch64_opcode_table) / sizeof(aarch64_opcode_table[0])"
        )
    )
    table_length -= 1  # last instruction is intentionally non-valid

    raw_instructions = [table[i] for i in range(table_length)]
    implicit_mapping = parse_mapping_file(MAPPING_INC_PATH)

    # import sys
    # sys.stdout = sys.__stdout__
    # sys.stderr = sys.__stderr__
    # import IPython
    # IPython.embed(colors="neutral")

    # Extract the data
    processed_instructions = []
    for raw_insn in raw_instructions:
        feature_set = str(raw_insn["avariant"]).split("<")[1].split(">")[0]

        # only support the core instructions for now
        # if feature_set != "aarch64_feature_v8":
        #     continue

        if (raw_insn["flags"] & 1 == 1):  # skip instructions with F_ALIAS flag (these are psuedo instructions)
            continue

        name = raw_insn["name"].string().upper().replace(".C", ".")  # B.C should be B.
        iclass = str(raw_insn["iclass"])

        # not branch_reg as the fuzzer expects control_flow instructions to
        # branch to labels only
        control_flow = ("branch" in iclass) and ("branch_reg" not in iclass)

        insn = {
            "name": name,
            "comment": iclass,
            "control_flow": control_flow,  
            "category": "general",  # for compatibility with get_spec.py
        }

        operands = get_operands(insn, raw_insn)
        implicit_operands = get_implicit_operands(insn, implicit_mapping)
        
        if operands is None or implicit_operands is None:
            continue

        # ignore instructions with labels as operands, as the fuzzer expects
        # all labels to be in control flow only
        if any(op["type_"] == "LABEL" for op in operands) and not control_flow:
            continue

        insn["operands"] = operands
        insn["implicit_operands"] = implicit_operands
        processed_instructions.append(insn)

    return sorted(processed_instructions, key=lambda i: i["name"])


def gdb_main():
    # Function to be executed when this script is run inside GDB
    json_output = get_aarch64_opcode_table_json()
    with open("aarch64_opcodes.json", "w") as outfile:
        json.dump(json_output, outfile, indent=2)
    print("JSON file generated.")


def download_mapping_file(url, path):
    response = requests.get(url)
    if response.status_code == 200:
        with open(path, "w") as f:
            f.write(response.text)
        print(f"Downloaded and saved to {path}")
    else:
        raise Exception(f"Failed to download file: HTTP {response.status_code}")


def find_libopcodes():
    # Find the libopcodes shared library file
    libopcodes_files = glob.glob("/usr/lib/aarch64-linux-gnu/libopcodes-*.so")
    if not libopcodes_files:
        raise FileNotFoundError("libopcodes shared library not found.")
    return libopcodes_files[0]


def check_libbinutils_dbg_installed():
    try:
        subprocess.run(
            ["dpkg", "-s", "libbinutils-dbg"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "libbinutils-dbg package is not installed. This package is essential for generating the instructions. Please install it to proceed."
        )


def standalone_main():
    # Function to be executed when this script is run outside GDB
    check_libbinutils_dbg_installed()  # Check if libbinutils-dbg is installed

    # Download the mapping file in standalone mode
    if not os.path.exists(MAPPING_INC_PATH):
        print("Downloading AArch64MappingInsn.inc file...")
        download_mapping_file(MAPPING_INC_URL, MAPPING_INC_PATH)

    print("Starting GDB with this Python script...")
    script_path = os.path.abspath(__file__)
    libopcodes_path = find_libopcodes()
    gdb_command = [
        "gdb",
        libopcodes_path,
        "--batch",
        "-ex",
        "set pagination off",
        "-ex",
        "set max-value-size 10000000",
        "-ex",
        f"source {script_path}",
    ]
    subprocess.run(gdb_command, check=True)


if __name__ == "__main__":
    if importlib.util.find_spec("gdb") is not None:
        gdb_main()
    else:
        standalone_main()
