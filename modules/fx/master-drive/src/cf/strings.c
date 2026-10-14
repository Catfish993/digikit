/* fx/master-drive: the label strings for the master page's repurposed knob.
 *
 * patchkit places this object in cf.expansion (the core/expansion loader copies it to RAM before the firmware
 * runs) and writes the addresses of these two strings into the parameter descriptor row of the master
 * compressor's Sidechain Filter: the long name shown while the knob turns, and the short label of the page.
 * Both must stay within the length of the strings they replace (16 and 3 characters in the stock firmware are
 * the displayed widths; the short label is also checked on the framebuffer). */

const char md_name_long[] = "Master Drive";
const char md_name_short[] = "DRV";
