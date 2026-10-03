#!/usr/bin/env python3
"""Exercise assembled reader/qualifier instructions with synthetic CLK inputs.

This small instruction model covers only operations used by these programs.
It verifies digital sequencing, not pad voltages, input synchronizers, physical
setup/hold margins, or the independent RW guard/writer hardware.
"""
import re
import sys
from pathlib import Path


def program(header, name):
    body = re.search(rf"{name}_program_instructions\[\] = \{{(.*?)\}};", header, re.S)
    assert body, name
    return [int(word, 16) for word in re.findall(r"0x([0-9a-f]{4}),", body[1])]


class Machine:
    def __init__(self, code, jump_pin, image=None, shifts=27):
        self.code, self.jump_pin = code, jump_pin
        self.pc = self.delay = self.x = self.osr = 0
        self.y = shifts - 1
        self.tx = [] if image is None else [(~image) & 0xffffffff]

    def step(self, pins, irq):
        if self.delay:
            self.delay -= 1
            return irq, {}, []
        word = self.code[self.pc]
        outputs = {3: (word >> 11) & 1} if word & 0x1000 else {}
        next_pc = (self.pc + 1) % len(self.code)
        op, arg = word >> 13, word & 0xff
        events = []
        if op == 0:  # JMP
            condition, target = arg >> 5, arg & 31
            if condition == 0:
                take = True
            elif condition == 2:
                take = self.x != 0
                self.x = (self.x - 1) & 0xffffffff
            elif condition == 6:
                take = bool(pins & (1 << self.jump_pin))
            else:
                raise AssertionError(f"Unsupported JMP condition {condition}")
            if take:
                next_pc = target
        elif op == 1:  # WAIT GPIO or IRQ
            polarity, source, index = arg >> 7, (arg >> 5) & 3, arg & 31
            assert source in (0, 2)
            value = bool((pins if source == 0 else irq) & (1 << index))
            if value != bool(polarity):
                return irq, outputs, events
            if source == 2 and polarity:
                irq &= ~(1 << index)
        elif op == 3:  # OUT PINDIRS, 1; right shift, no autopull
            assert arg == 0x81
            outputs[2] = self.osr & 1
            self.osr >>= 1
        elif op == 4:  # PULL BLOCK
            assert arg == 0xa0
            if not self.tx:
                return irq, outputs, events
            self.osr = self.tx.pop(0)
        elif op == 5:  # MOV X,Y or NOP (MOV Y,Y)
            assert arg in (0x22, 0x42)
            if arg == 0x22:
                self.x = self.y
        elif op == 6:  # IRQ SET/CLEAR, no relative index or wait
            assert arg in (0, 4, 5, 0x45)
            if arg & 0x40:
                irq &= ~(1 << (arg & 7))
            else:
                irq |= 1 << arg
                events.append(arg)
        else:
            raise AssertionError(f"Unsupported instruction {word:04x}")
        self.pc, self.delay = next_pc, (word >> 8) & 7
        return irq, outputs, events


class Bus:
    def __init__(self, reader, qualifier, image, shifts=27):
        self.reader = Machine(reader, 1, image, shifts)
        self.qualifier = Machine(qualifier, 0)
        self.irq = 1 << 5  # An earlier clock must not consume the next image.
        self.directions = {2: 0, 3: 1}
        self.events = []

    def run(self, cycles, clk=0, rw=1):
        for _ in range(cycles):
            # SM2's writes win over SM0's writes in the same cycle.
            for machine in (self.reader, self.qualifier):
                self.irq, outputs, events = machine.step(clk | (rw << 1), self.irq)
                self.directions.update(outputs)
                self.events.extend(events)


def check(header):
    qualifier = program(header, "live_int_clear")
    for name in ("live_read_rising", "live_read_falling"):
        reader = program(header, name)
        assert len(reader) + len(qualifier) + len(program(header, "live_rw_guard")) <= 32
        for width in range(1, 9):
            bus = Bus(reader, qualifier, 0x180001d)
            bus.run(20)
            assert bus.directions == {2: 0, 3: 0} and bus.reader.x == 26
            assert not bus.irq & (1 << 5), "Stale clock advanced a new image"
            bus.run(width, clk=1)
            bus.run(30)
            assert bus.directions == {2: 0, 3: 0}
            assert bus.reader.x == 26 and not bus.events, f"Accepted {width}-cycle spike"

        for image in (0x180001d, 0x400061d, 0x480061d, 0x500061d):
            bus = Bus(reader, qualifier, image)
            bus.run(20)
            for bit in range(27):
                # Isolated short pulses cannot consume bits or clear INT.
                before = dict(bus.directions)
                bus.run(2, clk=1)
                bus.run(30)
                assert bus.directions == before
                assert bus.directions[2] == (0 if image & (1 << bit) else 1)
                bus.run(150, clk=1)  # 1 us at 150 MHz
                bus.run(450)  # 3 us LOW
            assert bus.events.count(5) == 27
            assert bus.events.count(0) == 1 and 4 not in bus.events
            assert bus.directions == {2: 1, 3: 1}

        bus = Bus(reader, qualifier, 0, shifts=1)
        bus.run(20)
        assert bus.directions == {2: 1, 3: 0}
        bus.run(150, clk=1)
        bus.run(450)
        assert bus.events.count(0) == 1, "Completion must still need one real clock"

        bus = Bus(reader, qualifier, 0x180001d)
        bus.run(20)
        bus.run(150, clk=1, rw=0)
        bus.run(450, rw=0)
        assert 4 in bus.events and 0 not in bus.events, "Reader must cancel after RW transfer"
    print("Assembled PIO clock qualification, request and completion checks passed")


if __name__ == "__main__":
    check(Path(sys.argv[1]).read_text())
