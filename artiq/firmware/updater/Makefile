include ../include/generated/variables.mak
include $(MISOC_DIRECTORY)/software/common.mak

CFLAGS += \
	-I$(LIBUNWIND_DIRECTORY) \
	-I$(LIBUNWIND_DIRECTORY)/../unwinder/include

LDFLAGS += \
	-L../libunwind

RUSTFLAGS += -Cpanic=unwind

all:: updater.bin updater.fbi

.PHONY: $(RUSTOUT)/libupdater.a
$(RUSTOUT)/libupdater.a:
	$(cargo) --target-dir ./cargo \
	--manifest-path $(UPDATER_DIRECTORY)/Cargo.toml \
	--target $(UPDATER_DIRECTORY)/../$(CARGO_TRIPLE).json

updater.elf: $(RUSTOUT)/libupdater.a ksupport_data.o
	$(link) -T $(UPDATER_DIRECTORY)/../firmware.ld \
		-lunwind-vexriscv-bare -m elf32lriscv

ksupport_data.o: ../ksupport/ksupport.elf
	$(LD) -r -m elf32lriscv -b binary -o $@ $<

%.bin: %.elf
	$(objcopy) -O binary

%.fbi: %.bin
	$(mscimg) -f --little
