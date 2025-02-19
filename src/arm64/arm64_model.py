"""
File:

Copyright (C) Microsoft Corporation
SPDX-License-Identifier: MIT
"""
import numpy as np
import unicorn as uni
import unicorn.arm64_const as ucc
from model import UnicornModel, UnicornSeq, UnicornSpec, UnicornBpas, BaseTaintTracker
from interfaces import Input
from arm64.arm64_target_desc import ARMTargetDesc, ARM64UnicornTargetDesc

REG64_MASK = np.uint64(pow(2, 64) - 1)  # type: ignore


class ARM64UnicornModel(UnicornModel):
    """
    Base class that serves as main interface.
    Load inputs and executes the test case on AARCH64
    """

    def __init__(self, sandbox_base, code_start):
        self.target_desc = ARM64UnicornTargetDesc()
        self.architecture = (uni.UC_ARCH_ARM64, uni.UC_MODE_ARM)
        super().__init__(sandbox_base, code_start)

    def _load_input(self, input_: Input):
        """
        Set registers and stack before starting the emulation
        """
        # Set memory:
        # - initialize overflows with zeroes
        self.emulator.mem_write(self.lower_overflow_base, self.overflow_region_values)
        self.emulator.mem_write(self.upper_overflow_base, self.overflow_region_values)

        # - sandbox pages
        self.emulator.mem_write(self.sandbox_base, input_.get_memory().tobytes())

        # Set values in registers
        regs = self.target_desc.registers
        flags = self.target_desc.flags_register
        reg_init_address = self.sandbox_base + self.MAIN_REGION_SIZE + self.FAULTY_REGION_SIZE
        for i, value in enumerate(input_.get_registers()):
            if regs[i] == flags:
                value = (value << np.uint64(28)) % REG64_MASK   # type: ignore
            self.emulator.reg_write(regs[i], value)

            # executor uses the lower bytes of the upper_overflow_region to initialize registers
            # we need to match it in the model
            self.emulator.mem_write(reg_init_address, value.tobytes())
            reg_init_address += 8
        self.emulator.mem_write(reg_init_address,
                                self.stack_base.to_bytes(8, byteorder='little', signed=False))

        # initialize machine registers
        self.emulator.reg_write(ucc.UC_ARM64_REG_SP, self.stack_base)
        self.emulator.reg_write(ucc.UC_ARM64_REG_X30, self.sandbox_base)

    def print_state(self, oneline: bool = False):

        def compressed(val: int):
            if val >= self.sandbox_base and val <= self.sandbox_base + 12288:
                return f"+0x{val - self.sandbox_base:<15x}"
            elif val >= self.sandbox_base - self.OVERFLOW_REGION_SIZE and val < self.sandbox_base:
                return f"+0x{val - self.sandbox_base:<15x}"
            else:
                return f"0x{val:<16x}"

        emulator = self.emulator
        x0 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X0))
        x1 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X1))
        x2 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X2))
        x3 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X3))
        x4 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X4))
        x5 = compressed(emulator.reg_read(ucc.UC_ARM64_REG_X5))

        if not oneline:
            print("\n\nRegisters:")
            print(f"X0: {x0}")
            print(f"X1: {x1}")
            print(f"X2: {x2}")
            print(f"X3: {x3}")
            print(f"X4: {x4}")
            print(f"X5: {x5}")
        else:
            print(f"  x0={x0} "
                  f"x1={x1} "
                  f"x2={x2} \n"
                  f"  x3={x3} "
                  f"x4={x4} "
                  f"x5={x5} \n"
                  f"  nzcv={emulator.reg_read(ucc.UC_ARM64_REG_NZCV)>>28:0b}")


class ARMTaintTracker(BaseTaintTracker):
    # ISA-specific fields
    _registers = [
        ucc.UC_ARM64_REG_X0, ucc.UC_ARM64_REG_X1, ucc.UC_ARM64_REG_X2,
        ucc.UC_ARM64_REG_X3, ucc.UC_ARM64_REG_X4, ucc.UC_ARM64_REG_X5,
        ucc.UC_ARM64_REG_NZCV
    ]

    def __init__(self, initial_observations, sandbox_base=0):
        super().__init__(initial_observations, sandbox_base=sandbox_base)

        # ISA-specific field setup
        self.target_desc = ARMTargetDesc()
        self.unicorn_target_desc = ARM64UnicornTargetDesc()


# ==================================================================================================
# Implementation of Execution Clauses
# ==================================================================================================
class ARM64UnicornSeq(UnicornSeq, ARM64UnicornModel):
    pass


class ARM64UnicornSpec(UnicornSpec, ARM64UnicornModel):
    pass


class ARM64UnicornBpas(UnicornBpas, ARM64UnicornModel):
    pass
