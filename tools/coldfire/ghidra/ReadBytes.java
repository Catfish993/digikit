// Dump raw bytes at given addresses, for comparing against a local image file
// at the same load offset. Args: <outfile> <len> <hexaddr>...
//   tools/coldfire/ghidra.sh run ReadBytes.java /tmp/out.txt 16 0x4002eabe 0x400d4fb8
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import java.io.PrintWriter;

public class ReadBytes extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        PrintWriter w = new PrintWriter(args[0]);
        int len = Integer.decode(args[1]);
        for (int i = 2; i < args.length; i++) {
            Address a = toAddr(Long.decode(args[i]));
            byte[] b = new byte[len];
            try {
                currentProgram.getMemory().getBytes(a, b);
                StringBuilder hex = new StringBuilder();
                for (byte x : b) hex.append(String.format("%02x", x));
                w.println(args[i] + " " + hex);
            } catch (Exception e) {
                w.println(args[i] + " ERROR " + e.getMessage());
            }
        }
        w.close();
        println("wrote " + args[0]);
    }
}
