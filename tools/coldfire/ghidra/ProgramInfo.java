// Identify the program actually sitting in the Ghidra project: executable MD5,
// executable path, image base, and memory block ranges. Args: <outfile>
//   tools/coldfire/ghidra.sh run ProgramInfo.java /tmp/out.txt
import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;
import java.io.PrintWriter;

public class ProgramInfo extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        PrintWriter w = new PrintWriter(args[0]);
        w.println("name: " + currentProgram.getName());
        w.println("executablePath: " + currentProgram.getExecutablePath());
        w.println("executableMD5: " + currentProgram.getExecutableMD5());
        w.println("executableSHA256: " + currentProgram.getExecutableSHA256());
        w.println("imageBase: " + currentProgram.getImageBase());
        w.println("languageID: " + currentProgram.getLanguageID());
        w.println("compilerSpecID: " + currentProgram.getCompilerSpec().getCompilerSpecID());
        w.println("blocks:");
        for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
            w.println(String.format("  %-12s %s - %s  size=0x%x  perms=%s%s%s",
                b.getName(), b.getStart(), b.getEnd(), b.getSize(),
                b.isRead() ? "r" : "-", b.isWrite() ? "w" : "-", b.isExecute() ? "x" : "-"));
        }
        w.close();
        println("wrote " + args[0]);
    }
}
