// Real cross-references, including the PC-relative calls a byte search misses.
//   tools/coldfire/ghidra.sh run Callers.java <outfile> <hexaddr>...
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import java.io.PrintWriter;

public class Callers extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        PrintWriter w = new PrintWriter(args[0]);
        for (int i = 1; i < args.length; i++) {
            Address a = toAddr(Long.decode(args[i]));
            Function tf = getFunctionContaining(a);
            w.println(String.format("=== refs to %s  (fn %s) ===", args[i],
                tf == null ? "none" : tf.getEntryPoint().toString()));
            int n = 0;
            for (Reference r : currentProgram.getReferenceManager().getReferencesTo(a)) {
                Address from = r.getFromAddress();
                Function cf = getFunctionContaining(from);
                w.println(String.format("   %s  in fn %s  (%s)", from,
                    cf == null ? "?" : cf.getEntryPoint().toString(), r.getReferenceType()));
                if (++n >= 40) break;
            }
            if (n == 0) w.println("   (none)");
        }
        w.close();
        println("ok");
    }
}
