// Decompile one or more functions by address. Args: <outfile> <hexaddr>...
//   tools/coldfire/ghidra.sh run Decompile.java /tmp/out.txt 0x4017a0e4 0x40179fe0
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import java.io.PrintWriter;

public class Decompile extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        PrintWriter w = new PrintWriter(args[0]);
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        for (int i = 1; i < args.length; i++) {
            long t = Long.decode(args[i]);
            Function f = getFunctionContaining(toAddr(t));
            w.println("========== " + args[i] + " (fn "
                      + (f == null ? "none" : f.getEntryPoint()) + ") ==========");
            if (f == null) continue;
            DecompileResults r = di.decompileFunction(f, 90, monitor);
            w.println(r.decompileCompleted() ? r.getDecompiledFunction().getC()
                                             : "FAILED: " + r.getErrorMessage());
        }
        di.dispose();
        w.close();
        println("wrote " + args[0]);
    }
}
