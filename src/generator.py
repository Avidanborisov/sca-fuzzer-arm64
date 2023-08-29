"""
File: Test Case Generation

Copyright (C) Microsoft Corporation
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import random
import abc
import re
from typing import List, Dict
from subprocess import CalledProcessError, run
from collections import OrderedDict

from .isa_loader import InstructionSet
from .interfaces import Generator, TestCase, Operand, RegisterOperand, FlagsOperand, \
    MemoryOperand, ImmediateOperand, AgenOperand, LabelOperand, OT, Instruction, BasicBlock, \
    Function, OperandSpec, InstructionSpec, CondOperand, TargetDesc, Actor, ActorType
from .util import NotSupportedException, Logger
from .config import CONF


# Helpers
class GeneratorException(Exception):
    pass


class AsmParserException(Exception):

    def __init__(self, line_number, explanation):
        msg = "Could not parse line " + str(line_number + 1) + "\n  Reason: " + explanation
        super().__init__(msg)


def parser_assert(condition: bool, line_number: int, explanation: str):
    if not condition:
        logger = Logger()
        logger.error(
            f"asm_parser: Could not parse line {line_number + 1}\n"
            f"       Reason: {explanation}",
            print_tb=True)


# ==================================================================================================
# Generator Interface
# ==================================================================================================
class Pass(abc.ABC):

    @abc.abstractmethod
    def run_on_test_case(self, test_case: TestCase) -> None:
        pass


class Printer(abc.ABC):
    prologue_template: List[str]
    epilogue_template: List[str]

    @abc.abstractmethod
    def print(self, test_case: TestCase, outfile: str) -> None:
        pass


class ConfigurableGenerator(Generator, abc.ABC):
    """
    The interface description for Generator classes.
    """
    instruction_set: InstructionSet
    test_case: TestCase
    passes: List[Pass]  # set by subclasses
    printer: Printer  # set by subclasses
    target_desc: TargetDesc  # set by subclasses

    LOG: Logger  # name capitalized to make logging easily distinguishable from the main logic

    def __init__(self, instruction_set: InstructionSet, seed: int):
        super().__init__(instruction_set, seed)
        self.LOG = Logger()
        self.LOG.dbg_gen_instructions(instruction_set.instructions)
        self.control_flow_instructions = \
            [i for i in self.instruction_set.instructions if i.control_flow]
        assert self.control_flow_instructions or CONF.max_bb_per_function <= 1, \
               "The instruction set is insufficient to generate a test case"

        self.non_control_flow_instructions = \
            [i for i in self.instruction_set.instructions if not i.control_flow]
        assert self.non_control_flow_instructions, \
            "The instruction set is insufficient to generate a test case"

        self.non_memory_access_instructions = \
            [i for i in self.non_control_flow_instructions if not i.has_mem_operand]
        if CONF.avg_mem_accesses != 0:
            memory_access_instructions = \
                [i for i in self.non_control_flow_instructions if i.has_mem_operand]
            self.load_instruction = [i for i in memory_access_instructions if not i.has_write]
            self.store_instructions = [i for i in memory_access_instructions if i.has_write]
            assert self.load_instruction or self.store_instructions, \
                "The instruction set does not have memory accesses while `avg_mem_accesses > 0`"
        else:
            self.load_instruction = []
            self.store_instructions = []

    def set_seed(self, seed: int) -> None:
        self._state = seed

    def create_test_case(self, asm_file: str, disable_assembler: bool = False) -> TestCase:
        self.test_case = TestCase(self._state)

        # set seeds
        if self._state == 0:
            self._state = random.randint(1, 1000000)
            self.LOG.inform("prog_gen",
                            f"Setting program_generator_seed to random value: {self._state}")
        random.seed(self._state)
        self._state += 1

        # create the main function
        default_actor = self.test_case.actors[0]
        func = self.generate_function(".function_0", default_actor, self.test_case)

        # fill the function with instructions
        self.add_terminators_in_function(func)
        self.add_instructions_in_function(func)

        # add it to the test case
        self.test_case.functions.append(func)

        # process the test case
        for p in self.passes:
            p.run_on_test_case(self.test_case)

        self.printer.print(self.test_case, asm_file)
        self.test_case.asm_path = asm_file

        if disable_assembler:
            return self.test_case

        bin_file = asm_file[:-4]
        obj_file = bin_file + ".o"
        self.assemble(asm_file, obj_file, bin_file)
        self.test_case.bin_path = bin_file
        self.test_case.obj_path = obj_file

        self.map_addresses(self.test_case, obj_file)

        return self.test_case

    @staticmethod
    def assemble(asm_file: str, obj_file: str, bin_file: str) -> None:
        """Assemble the test case into a stripped binary"""

        def pretty_error_msg(error_msg):
            with open(asm_file, "r") as f:
                lines = f.read().split("\n")

            msg = "Error appeared while assembling the test case:\n"
            for line in error_msg.split("\n"):
                line = line.removeprefix(asm_file + ":")
                line_num_str = re.search(r"(\d+):", line)
                if not line_num_str:
                    msg += line
                else:
                    parsed = lines[int(line_num_str.group(1)) - 1]
                    msg += f"\n  Line {line}\n    (the line was parsed as {parsed})"
            return msg

        # make sure that all function labels are exposed in the object file
        # by adding a NOP before each function label (this makes the function label
        # into the only label that points to the next instruction)
        patched_asm_file = asm_file + ".patched"
        function_found = False
        enter_found = False
        with open(asm_file, "r") as f:
            with open(patched_asm_file, "w") as patched:
                for line in f:
                    line = line.strip()
                    if not enter_found:
                        if line == ".test_case_enter:":
                            enter_found = True
                        patched.write(line + "\n")
                        continue

                    if not function_found and line and line[0] != "#" \
                       and "function" not in line and "section" not in line:
                        patched.write(".global .global_function_0\n")
                        patched.write(".global_function_0:\n")
                        patched.write(".function_0:\n")
                        function_found = True
                    elif line.startswith(".function_"):
                        name = line[1:-1]
                        patched.write(".global .global_" + name + "\n")
                        patched.write(".global_" + name + ":\n")
                        function_found = True
                    patched.write(line + "\n")

        try:
            out = run(
                f"as {patched_asm_file} -o {obj_file}", shell=True, check=True, capture_output=True)
        except CalledProcessError as e:
            error_msg = e.stderr.decode()
            if "Assembler messages:" in error_msg:
                print(pretty_error_msg(error_msg))
            else:
                print(error_msg)
            raise e
        finally:
            pass
            # run(f"rm {patched_asm_file}", shell=True, check=True)

        output = out.stderr.decode()
        if "Assembler messages:" in output:
            print("WARNING: [generator]" + pretty_error_msg(output))

        run(f"cp {obj_file} {bin_file}", shell=True, check=True)
        run(f"strip --remove-section=.note.gnu.property {bin_file}", shell=True, check=True)
        run(f"objcopy {bin_file} -O binary {bin_file}", shell=True, check=True)

    def load(self, asm_file: str) -> TestCase:
        test_case = TestCase(0)
        test_case.asm_path = asm_file

        # prepare regexes
        re_redundant_spaces = re.compile(r"(?<![a-zA-Z0-9]) +")

        # prepare a map of all instruction specs
        instruction_map: Dict[str, List[InstructionSpec]] = {}
        for spec in self.instruction_set.instructions:
            if spec.name in instruction_map:
                instruction_map[spec.name].append(spec)
            else:
                instruction_map[spec.name] = [spec]

            # add an entry for direct opcodes
            opcode_spec = InstructionSpec()
            opcode_spec.name = "OPCODE"
            opcode_spec.category = "OPCODE"
            instruction_map["OPCODE"] = [opcode_spec]

            # entry for symbols
            symbol_spec = InstructionSpec()
            symbol_spec.name = "SYMBOL"
            symbol_spec.category = "SYMBOL"
            symbol_spec.operands = [OperandSpec([], OT.IMM, False, False)]
            instruction_map["SYMBOL"] = [symbol_spec]

        # load the text and clean it up
        lines = []
        started = False
        finished = False
        with open(asm_file, "r") as f:
            for i, line in enumerate(f):
                # remove extra spaces
                line = line.strip()
                line = re_redundant_spaces.sub("", line)

                # skip comments and empty lines
                if not line or line[0] in ["", "#", "/"]:
                    continue

                # skip footer and header
                if not started:
                    started = (line == ".test_case_enter:")
                    if not line[0] == ".":
                        parser_assert(started, i, "Found instructions before .test_case_enter")
                    continue
                if line == ".test_case_exit:":
                    finished = True
                    continue
                parser_assert(not finished, i, "Found instructions after .test_case_exit")

                lines.append(line)

        # map lines to functions and basic blocks
        current_function = ""
        current_bb = ""
        current_actor = ""
        autogenerated_bb = False
        test_case_map: Dict[str, Dict[str, List[str]]] = OrderedDict()
        function_owners: Dict[str, str] = {}
        for i, line in enumerate(lines):
            # directives - ignored
            if line.startswith(".global"):
                continue

            # section start
            if line.startswith(".section"):
                words = line.split()
                assert len(words) == 2
                if words[1] == "exit":
                    continue  # exit section does not represent any actor
                current_actor = words[1]
                current_function = ""
                current_bb = ""
                continue
            parser_assert(current_actor != "", i, "Missing actor declaration (missing .section)")

            # function start
            if line.startswith(".function_"):
                assert line[-1] == ":", f"Invalid function header: {line}"
                current_function = line[:-1]
                test_case_map[current_function] = OrderedDict()
                function_owners[current_function] = current_actor

                autogenerated_bb = True
                current_bb = ".bb_" + current_function.removeprefix(".function_") + ".entry"
                test_case_map[current_function][current_bb] = []
                continue

            # implicit declaration of the main function
            if not current_function and not test_case_map:
                current_function = ".function_0"
                test_case_map[current_function] = OrderedDict()
                function_owners[current_function] = current_actor

                autogenerated_bb = True
                current_bb = ".bb_" + current_function.removeprefix(".function_") + ".entry"
                test_case_map[current_function][current_bb] = []
            parser_assert(current_function != "", i, "Missing function declaration")

            # opcode
            if line[:4] == ".bcd " or line[:5] in [".byte", ".long", ".quad"] \
               or line[6:] in [".value", ".2byte", ".4byte", ".8byte"]:
                assert current_bb
                test_case_map[current_function][current_bb].append("OPCODE")
                continue

            # symbols
            if line.startswith(".symbol"):
                parser_assert(current_bb != "", i, "Symbol declared outside of a basic block")
                words = line.split(":")
                parser_assert(len(words) == 2, i, "Invalid symbol declaration")
                parser_assert(words[1].upper() == "NOP", i, "Symbol must end with NOP")
                subwords = words[0].split(".")
                parser_assert(len(subwords) == 3, i, f"Invalid symbol: {line}")
                symbol_id = self.target_desc.symbol_ids[subwords[2]]
                test_case_map[current_function][current_bb].append("SYMBOL " + str(symbol_id))
                continue

            # basic block
            if line.startswith("."):
                assert line[-1] == ":", f"Invalid basic block header: {line}"
                # remove empty default BBs
                if autogenerated_bb and not test_case_map[current_function][current_bb]:
                    del test_case_map[current_function][current_bb]

                autogenerated_bb = False
                current_bb = line[:-1]
                if current_bb not in test_case_map[current_function]:
                    test_case_map[current_function][current_bb] = []
                continue

            # instruction
            parser_assert(current_bb != "", i, "Missing basic block declaration")
            test_case_map[current_function][current_bb].append(line)

        # create actors
        actor_names: Dict[str, Actor] = {}
        for actor_label in sorted(set(function_owners.values())):
            words = actor_label.split(".")
            assert len(words) == 3, f"Invalid actor label: {actor_label}"
            subwords = words[2].split("_")
            assert len(subwords) == 2, f"Invalid actor label: {actor_label}"

            id_ = int(subwords[0])
            if subwords[1] == "host":
                type_ = ActorType.HOST
            elif subwords[1] == "guest":
                type_ = ActorType.GUEST
            else:
                parser_assert(False, 0, f"Invalid actor type: {subwords[1]}")

            actor = Actor(type_, id_)  # type: ignore
            assert id_ not in test_case.actors or test_case.actors[id_] == actor, "Duplicate actor"
            test_case.actors[id_] = actor
            actor_names[actor_label] = actor

        # parse lines and create their object representations
        line_id = 1
        for func_name, bbs in test_case_map.items():
            # print(func_name)
            line_id += 1
            actor = actor_names[function_owners[func_name]]
            func = Function(func_name, actor)
            test_case.functions.append(func)

            for bb_name, lines in bbs.items():
                # print(">>", bb_name)
                line_id += 1
                bb = BasicBlock(bb_name)
                func.append(bb)

                terminators_started = False
                for line in lines:
                    # print(f"    {line}")
                    line_id += 1
                    inst = self.parse_line(line, line_id, instruction_map)
                    if inst.control_flow and not self.target_desc.is_call(inst):
                        terminators_started = True
                        bb.insert_terminator(inst)
                    else:
                        parser_assert(not terminators_started, line_id,
                                      "Terminator not at the end of BB")
                        bb.insert_after(bb.get_last(), inst)

        # connect basic blocks
        bb_names = {bb.name.upper(): bb for func in test_case for bb in func}
        bb_names[".TEST_CASE_EXIT"] = test_case.exit
        previous_bb = None
        for func in test_case:
            for bb in func:
                # fallthrough
                if previous_bb:  # skip the first BB
                    # there is a fallthrough only if the last terminator is not a direct jump
                    if not previous_bb.terminators or \
                       not self.target_desc.is_unconditional_branch(previous_bb.terminators[-1]):
                        previous_bb.successors.append(bb)
                previous_bb = bb

                # taken branches
                for terminator in bb.terminators:
                    for op in terminator.operands:
                        if isinstance(op, LabelOperand):
                            successor = bb_names[op.value]
                            bb.successors.append(successor)

            # last BB always falls through to the exit
            func[-1].successors.append(func.exit)

        bin_file = asm_file[:-4]
        obj_file = bin_file + ".o"
        self.assemble(asm_file, obj_file, bin_file)
        test_case.bin_path = bin_file
        test_case.obj_path = obj_file

        self.map_addresses(test_case, obj_file)

        return test_case

    @abc.abstractmethod
    def parse_line(self, line: str, line_num: int,
                   instruction_map: Dict[str, List[InstructionSpec]]) -> Instruction:
        pass

    @abc.abstractmethod
    def map_addresses(self, test_case: TestCase, obj_file: str) -> None:
        pass

    @abc.abstractmethod
    def generate_function(self, name: str, owner: Actor, parent: TestCase) -> Function:
        pass

    @abc.abstractmethod
    def generate_instruction(self, spec: InstructionSpec) -> Instruction:
        pass

    def generate_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        generators = {
            OT.REG: self.generate_reg_operand,
            OT.MEM: self.generate_mem_operand,
            OT.IMM: self.generate_imm_operand,
            OT.LABEL: self.generate_label_operand,
            OT.AGEN: self.generate_agen_operand,
            OT.FLAGS: self.generate_flags_operand,
            OT.COND: self.generate_cond_operand,
        }
        return generators[spec.type](spec, parent)

    @abc.abstractmethod
    def generate_reg_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_mem_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_imm_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_label_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_agen_operand(self, _: OperandSpec, __: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_flags_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def generate_cond_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        pass

    @abc.abstractmethod
    def add_terminators_in_function(self, func: Function):
        pass

    @abc.abstractmethod
    def add_instructions_in_function(self, func: Function):
        pass


# ==================================================================================================
# ISA-independent Generators
# ==================================================================================================
class RandomGenerator(ConfigurableGenerator, abc.ABC):
    """
    Implements an ISA-independent logic of random test case generation.
    Subclasses are responsible for the ISA-specific parts.
    """
    had_recent_memory_access: bool = False

    def __init__(self, instruction_set: InstructionSet, seed: int):
        super().__init__(instruction_set, seed)
        uncond_name = self.get_unconditional_jump_instruction().name.lower()
        self.cond_branches = \
            [i for i in self.control_flow_instructions if i.name.lower() != uncond_name]

    def generate_function(self, label: str, owner: Actor, parent: TestCase):
        """ Generates a random DAG of basic blocks within a function """
        func = Function(label, owner)

        # Define the maximum allowed number of successors for any BB
        if self.instruction_set.has_conditional_branch:
            max_successors = CONF.max_successors_per_bb if CONF.max_successors_per_bb < 2 else 2
            min_successors = CONF.min_successors_per_bb \
                if CONF.min_successors_per_bb < max_successors else max_successors
        else:
            max_successors = 1
            min_successors = 1

        # Create basic blocks
        node_count = random.randint(CONF.min_bb_per_function, CONF.max_bb_per_function)
        func_name = label.removeprefix(".function_")
        nodes = [BasicBlock(f".bb_{func_name}.{i}") for i in range(node_count)]

        # Connect BBs into a graph
        for i in range(node_count):
            current_bb = nodes[i]

            # the last node has only one successor - exit
            if i == node_count - 1:
                current_bb.successors = [func.exit]
                break

            # the rest of the node have a random number of successors
            successor_count = random.randint(min_successors, max_successors)
            if successor_count + i > node_count:
                # the number is adjusted to the position when close to the end
                successor_count = node_count - i

            # one of the targets (the first successor) is always the next node - to avoid dead code
            current_bb.successors.append(nodes[i + 1])

            # all other successors are random, selected from next nodes
            options = nodes[i + 2:]
            options.append(func.exit)
            for j in range(1, successor_count):
                target = random.choice(options)
                options.remove(target)
                current_bb.successors.append(target)

        # Function returns are not yet supported
        # hence all functions end with an unconditional jump to the exit
        func.exit.terminators = [
            self.get_unconditional_jump_instruction().add_op(LabelOperand(parent.exit.name))
        ]

        # Finalize the function
        func.extend(nodes)
        return func

    def generate_instruction(self, spec: InstructionSpec) -> Instruction:
        # fill up with random operands, following the spec
        inst = Instruction.from_spec(spec)

        # generate explicit operands
        for operand_spec in spec.operands:
            operand = self.generate_operand(operand_spec, inst)
            inst.operands.append(operand)

        # generate implicit operands
        for operand_spec in spec.implicit_operands:
            operand = self.generate_operand(operand_spec, inst)
            inst.implicit_operands.append(operand)

        return inst

    def generate_reg_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        reg_type = spec.values[0]
        if reg_type == 'GPR':
            choices = self.target_desc.registers[spec.width]
        elif reg_type == "SIMD":
            choices = self.target_desc.simd_registers[spec.width]
        else:
            choices = spec.values

        if not CONF.avoid_data_dependencies:
            reg = random.choice(choices)
            return RegisterOperand(reg, spec.width, spec.src, spec.dest)

        if parent.latest_reg_operand and parent.latest_reg_operand.value in choices:
            return parent.latest_reg_operand

        reg = random.choice(choices)
        op = RegisterOperand(reg, spec.width, spec.src, spec.dest)
        parent.latest_reg_operand = op
        return op

    def generate_mem_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        if spec.values:
            address_reg = random.choice(spec.values)
        else:
            address_reg = random.choice(self.target_desc.registers[64])
        return MemoryOperand(address_reg, spec.width, spec.src, spec.dest)

    def generate_imm_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        if spec.values:
            if spec.values[0] == "bitmask":
                # FIXME: this implementation always returns the same bitmask
                # make it random
                value = str(pow(2, spec.width) - 2)
            else:
                assert "[" in spec.values[0], spec.values
                range_ = spec.values[0][1:-1].split("-")
                if range_[0] == "":
                    range_ = range_[1:]
                    range_[0] = "-" + range_[0]
                assert len(range_) == 2
                value = str(random.randint(int(range_[0]), int(range_[1])))
        else:
            value = str(random.randint(pow(2, spec.width - 1) * -1, pow(2, spec.width - 1) - 1))
        return ImmediateOperand(value, spec.width)

    def generate_label_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        return LabelOperand("")  # the actual label will be set in add_terminators_in_function

    def generate_agen_operand(self, spec: OperandSpec, __: Instruction) -> Operand:
        n_operands = random.randint(1, 3)
        reg1 = random.choice(self.target_desc.registers[64])
        if n_operands == 1:
            return AgenOperand(reg1, spec.width)

        reg2 = random.choice(self.target_desc.registers[64])
        if n_operands == 2:
            return AgenOperand(reg1 + " + " + reg2, spec.width)

        imm = str(random.randint(0, pow(2, 16) - 1))
        return AgenOperand(reg1 + " + " + reg2 + " + " + imm, spec.width)

    def generate_flags_operand(self, spec: OperandSpec, parent: Instruction) -> Operand:
        cond_op = parent.get_cond_operand()
        if not cond_op:
            return FlagsOperand(spec.values)

        flag_values = self.target_desc.branch_conditions[cond_op.value]
        if not spec.values:
            return FlagsOperand(flag_values)

        # combine implicit flags with the condition
        merged_flags = []
        for flag_pair in zip(flag_values, spec.values):
            if "undef" in flag_pair:
                merged_flags.append("undef")
            elif "r/w" in flag_pair:
                merged_flags.append("r/w")
            elif "w" in flag_pair:
                if "r" in flag_pair:
                    merged_flags.append("r/w")
                else:
                    merged_flags.append("w")
            elif "cw" in flag_pair:
                if "r" in flag_pair:
                    merged_flags.append("r/cw")
                else:
                    merged_flags.append("cw")
            elif "r" in flag_pair:
                merged_flags.append("r")
            else:
                merged_flags.append("")
        return FlagsOperand(merged_flags)

    def generate_cond_operand(self, spec: OperandSpec, _: Instruction) -> Operand:
        cond = random.choice(list(self.target_desc.branch_conditions))
        return CondOperand(cond)

    def add_terminators_in_function(self, func: Function):

        def add_fallthrough(bb: BasicBlock, destination: BasicBlock):
            # create an unconditional branch and add it
            terminator = self.get_unconditional_jump_instruction()
            terminator.operands = [LabelOperand(destination.name)]
            bb.terminators.append(terminator)

        for bb in func:
            if len(bb.successors) == 0:
                # Return instruction
                continue

            elif len(bb.successors) == 1:
                # Unconditional branch
                dest = bb.successors[0]
                if dest == func.exit:
                    # DON'T insert a branch to the exit
                    # the last basic block always falls through implicitly
                    continue
                add_fallthrough(bb, dest)

            elif len(bb.successors) == 2:
                # Conditional branch
                spec = random.choice(self.cond_branches)
                terminator = self.generate_instruction(spec)
                label = terminator.get_label_operand()
                assert label
                label.value = bb.successors[0].name
                bb.terminators.append(terminator)

                add_fallthrough(bb, bb.successors[1])
            else:
                # Indirect jump
                raise NotSupportedException()

    def add_instructions_in_function(self, func: Function):
        # evenly fill all BBs with random instructions
        bb_list = func[:]
        for _ in range(0, CONF.program_size):
            bb = random.choice(bb_list)
            spec = self._pick_random_instruction_spec()
            inst = self.generate_instruction(spec)
            bb.insert_after(bb.get_last(), inst)

    def _pick_random_instruction_spec(self) -> InstructionSpec:
        # ensure the requested avg. number of mem. accesses
        search_for_memory_access = False
        memory_access_probability = CONF.avg_mem_accesses / CONF.program_size
        if CONF.generate_memory_accesses_in_pairs:
            memory_access_probability = 1 if self.had_recent_memory_access else \
                (CONF.avg_mem_accesses / 2) / (CONF.program_size - CONF.avg_mem_accesses / 2)

        if random.random() < memory_access_probability:
            search_for_memory_access = True
            self.had_recent_memory_access = not self.had_recent_memory_access

        if self.store_instructions:
            search_for_store = random.random() < 0.5  # 50% probability of stores
        else:
            search_for_store = False

        # select a random instruction spec for generation
        if not search_for_memory_access:
            return random.choice(self.non_memory_access_instructions)

        if search_for_store:
            return random.choice(self.store_instructions)

        return random.choice(self.load_instruction)

    @abc.abstractmethod
    def get_return_instruction(self) -> Instruction:
        pass

    @abc.abstractmethod
    def get_unconditional_jump_instruction(self) -> Instruction:
        pass
