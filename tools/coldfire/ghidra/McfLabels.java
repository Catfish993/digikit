// Apply MCF5441x MMIO labels using facts from:
//   docs/contracts/mcf5441x-reference-v1.json (per-fact source ledger)
//   docs/refs/linux-m5441x-893e1178.md (torvalds/linux cross-check)
//   docs/refs/netburner-mcf5441x-layout.md (NetBurner NNDK cross-check)
// Creates the peripheral memory blocks first -- they fall outside the imported MAIN OS
// image's mapped range -- then places labels in a dedicated MCF5441X_MMIO namespace so a
// rerun never duplicates a label and never touches a user's own naming.
//   tools/coldfire/ghidra.sh run McfLabels.java [outfile]
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Namespace;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;
import ghidra.program.model.symbol.SourceType;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

public class McfLabels extends GhidraScript {
    static final String NAMESPACE = "MCF5441X_MMIO";

    // Per-instance bases for the repeating register blocks below. Facts and their
    // sources for every offset in buildLabels() are in
    // docs/contracts/mcf5441x-reference-v1.json and the two docs/refs/*.md files
    // this script's header cites; this table is a flat restatement for Ghidra's
    // Jython-less Java scripting, which has no JSON reader on the classpath by
    // default, not a new source of truth.
    static final long[] INTC_BASES = {0xFC048000L, 0xFC04C000L, 0xFC050000L};
    static final long[] PIT_BASES  = {0xFC080000L, 0xFC084000L, 0xFC088000L, 0xFC08C000L};
    static final long[] DTIM_BASES = {0xFC070000L, 0xFC074000L, 0xFC078000L, 0xFC07C000L};
    static final long[] UART_BASES = {0xEC070000L, 0xEC074000L};

    // {name, address, size} -- size is only used to size the backing memory block if
    // one does not already cover the address; it does not claim that many bytes are
    // all meaningful registers.
    static List<Object[]> buildLabels() {
        List<Object[]> l = new ArrayList<Object[]>();

        // Interrupt controllers 0-2: base+ICR0 (m5441xsim.h:36-49) plus
        // IMRH/IMRL/SIMR/CIMR (imr_offset_source_ref, simr_cimr_offset_source_ref
        // in the contract: sim5441x.h:534-566, intc-simr.c).
        for (int i = 0; i < INTC_BASES.length; i++) {
            long base = INTC_BASES[i];
            l.add(new Object[]{"INTC" + i + "_BASE", base, 0x4000L});
            l.add(new Object[]{"INTC" + i + "_ICR0", base + 0x40L, 0x40L});
            l.add(new Object[]{"INTC" + i + "_IMRH", base + 0x08L, 4L});
            l.add(new Object[]{"INTC" + i + "_IMRL", base + 0x0CL, 4L});
            l.add(new Object[]{"INTC" + i + "_SIMR", base + 0x1CL, 1L});
            l.add(new Object[]{"INTC" + i + "_CIMR", base + 0x1DL, 1L});
        }

        // PIT0-3 base (m5441xsim.h:104-114); PCSR/PMR/PCNTR offsets from
        // docs/refs/MCF5441X-notes.md section 4a (MCF5441XRM ch.38, primary source).
        for (int i = 0; i < PIT_BASES.length; i++) {
            long base = PIT_BASES[i];
            l.add(new Object[]{"PIT" + i + "_BASE", base, 0x4000L});
            l.add(new Object[]{"PIT" + i + "_PCSR", base, 2L});
            l.add(new Object[]{"PIT" + i + "_PMR", base + 0x02L, 2L});
            l.add(new Object[]{"PIT" + i + "_PCNTR", base + 0x04L, 2L});
        }

        // DTIM0-3 base (netburner sim5441x.h:1426) and register offsets
        // (register_offsets_source_ref in the contract: dma_timer.c, sim5441x.h:663-674).
        for (int i = 0; i < DTIM_BASES.length; i++) {
            long base = DTIM_BASES[i];
            l.add(new Object[]{"DTIM" + i + "_BASE", base, 0x4000L});
            l.add(new Object[]{"DTIM" + i + "_DTMR", base, 2L});
            l.add(new Object[]{"DTIM" + i + "_DTXMR", base + 0x02L, 1L});
            l.add(new Object[]{"DTIM" + i + "_DTER", base + 0x03L, 1L});
            l.add(new Object[]{"DTIM" + i + "_DTRR", base + 0x04L, 4L});
            l.add(new Object[]{"DTIM" + i + "_DTCR", base + 0x08L, 4L});
            l.add(new Object[]{"DTIM" + i + "_DTCN", base + 0x0CL, 4L});
        }

        // GPIO (m5441xsim.h:236-268)
        l.add(new Object[]{"GPIO_PODR_A", 0xEC094000L, 0x0CL});
        l.add(new Object[]{"GPIO_PDDR_A", 0xEC09400CL, 0x0CL});
        l.add(new Object[]{"GPIO_PPDSDR_A", 0xEC094018L, 0x0CL});
        l.add(new Object[]{"GPIO_PCLRR_A", 0xEC094024L, 0x0CL});

        // UART8/9 base (m5441xsim.h:161-171); USR/DAT (UTB) offsets from
        // docs/refs/linux-m5441x-893e1178.md's UART section.
        for (int i = 0; i < UART_BASES.length; i++) {
            long base = UART_BASES[i];
            String name = "UART" + (8 + i);
            l.add(new Object[]{name + "_BASE", base, 0x4000L});
            l.add(new Object[]{name + "_USR", base + 0x04L, 1L});
            l.add(new Object[]{name + "_DAT", base + 0x0CL, 1L});
        }

        // eSDHC base (m5441xsim.h:299-303); other offsets from unicorn-reference-final:emu/esdhc.py
        // (DSADDR..HOSTVER, RM ch.25), which this script does not otherwise modify.
        l.add(new Object[]{"ESDHC_BASE", 0xFC0CC000L, 0x100L});
        l.add(new Object[]{"ESDHC_XFERTYP", 0xFC0CC00CL, 4L});
        l.add(new Object[]{"ESDHC_PRSSTAT", 0xFC0CC024L, 4L});
        l.add(new Object[]{"ESDHC_SYSCTL", 0xFC0CC02CL, 4L});
        l.add(new Object[]{"ESDHC_IRQSTAT", 0xFC0CC030L, 4L});

        // eDMA base/SERQ/TCD_BASE from unicorn-reference-final:emu/edma.py, cross-checked against netburner
        // sim5441x.h:483-497,527,1404; ERQL/CINT from the same sim5441x.h edmastruct.
        // TCD35 (UART8 TX, unicorn-reference-final:emu/edma.py TX_CHAN/TX_VECTOR) fields use unicorn-reference-final:emu/edma.py's
        // TCD offsets, non-Kinetis order (CITER at +0x14, BITER at +0x1C).
        l.add(new Object[]{"EDMA_BASE", 0xFC044000L, 0x4000L});
        l.add(new Object[]{"EDMA_ERQL", 0xFC04400CL, 4L});
        l.add(new Object[]{"EDMA_SERQ", 0xFC044018L, 1L});
        l.add(new Object[]{"EDMA_CINT", 0xFC04401CL, 1L});
        l.add(new Object[]{"EDMA_TCD_BASE", 0xFC045000L, 0x1000L});
        long tcd35 = 0xFC045000L + 35L * 0x20L;
        l.add(new Object[]{"EDMA_TCD35_SADDR", tcd35, 4L});
        l.add(new Object[]{"EDMA_TCD35_NBYTES", tcd35 + 0x08L, 4L});
        l.add(new Object[]{"EDMA_TCD35_DADDR", tcd35 + 0x10L, 4L});
        l.add(new Object[]{"EDMA_TCD35_CITER", tcd35 + 0x14L, 2L});
        l.add(new Object[]{"EDMA_TCD35_BITER", tcd35 + 0x1CL, 2L});

        return l;
    }

    public void run() throws Exception {
        String[] args = getScriptArgs();
        PrintWriter w = args.length > 0 ? new PrintWriter(args[0]) : new PrintWriter(System.out);
        Memory mem = currentProgram.getMemory();
        SymbolTable symtab = currentProgram.getSymbolTable();
        Namespace ns = symtab.getNamespace(NAMESPACE, currentProgram.getGlobalNamespace());
        if (ns == null) {
            ns = symtab.createNameSpace(currentProgram.getGlobalNamespace(), NAMESPACE,
                                         SourceType.ANALYSIS);
        }
        int created = 0, blocks = 0, skipped = 0;
        for (Object[] row : buildLabels()) {
            String name = (String) row[0];
            long addr = (Long) row[1];
            long size = (Long) row[2];
            Address a = toAddr(addr);
            if (!ensureBlock(mem, a, size, w)) {
                blocks++;
            }
            if (labelExists(symtab, a, name, ns)) {
                skipped++;
                continue;
            }
            if (hasForeignUserLabel(symtab, a)) {
                w.println("skip " + name + " @ " + a + " -- a differently-sourced "
                          + "user label already exists there");
                skipped++;
                continue;
            }
            symtab.createLabel(a, name, ns, SourceType.ANALYSIS);
            w.println("label " + name + " @ " + a);
            created++;
        }
        w.println(String.format("done: %d labels created, %d blocks created, %d skipped",
                                 created, blocks, skipped));
        w.close();
        println("ok");
    }

    /** -> true if a block already covers `a`; otherwise creates one. */
    private boolean ensureBlock(Memory mem, Address a, long size, PrintWriter w) throws Exception {
        MemoryBlock existing = mem.getBlock(a);
        if (existing != null) {
            return true;
        }
        String blockName = "MCF5441X_" + a.toString().replace(":", "_");
        MemoryBlock b = mem.createUninitializedBlock(blockName, a, size, false);
        b.setRead(true);
        b.setWrite(true);
        b.setExecute(false);
        w.println("block " + blockName + " @ " + a + " size " + size);
        return false;
    }

    /** -> true if our own namespace already has this exact label here. */
    private boolean labelExists(SymbolTable symtab, Address a, String name, Namespace ns) {
        for (Symbol s : symtab.getSymbols(a)) {
            if (s.getName().equals(name) && s.getParentNamespace().equals(ns)) {
                return true;
            }
        }
        return false;
    }

    /** -> true if a symbol at `a` was named by the user or by any source
     * other than this script's own namespace/source type -- never
     * overwrite or shadow that. */
    private boolean hasForeignUserLabel(SymbolTable symtab, Address a) {
        for (Symbol s : symtab.getSymbols(a)) {
            if (s.getParentNamespace().getName().equals(NAMESPACE)) {
                continue;
            }
            if (s.getSource() == SourceType.USER_DEFINED || s.getSource() == SourceType.IMPORTED) {
                return true;
            }
        }
        return false;
    }
}
