/* freqadj -- read or drive the kernel clock frequency offset.
 *
 * The drift leg (tools/drift-leg.sh) uses this to amplify NTP-style
 * frequency discipline deterministically: CLOCK_MONOTONIC and
 * CLOCK_REALTIME follow the adjtimex frequency, CLOCK_MONOTONIC_RAW
 * does not, which is exactly the wedge a media clock on the RAW side
 * falls into. Thingino busybox ships no adjtimex applet, hence this
 * static tool. Stop ntpd first or it will fight the setting.
 *
 *   freqadj get           print current offset: "<raw> <ppm>"
 *   freqadj add <ppm>     add ppm (may be negative); prints the raw
 *                         value to restore: "old <raw> new <raw>"
 *   freqadj setraw <raw>  restore an exact saved raw value
 *
 * raw is the kernel's scaled ppm (ppm * 65536), range +-500ppm.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/timex.h>

static long get_freq(void)
{
	struct timex tx;

	memset(&tx, 0, sizeof(tx));
	if (adjtimex(&tx) < 0) {
		perror("adjtimex");
		exit(1);
	}
	return tx.freq;
}

static void set_freq(long raw)
{
	struct timex tx;

	/* Clear the PLL: a stopped ntpd leaves STA_PLL set with an
	 * in-flight offset, and the kernel keeps slewing that offset as
	 * a decaying frequency term ON TOP of tx.freq (measured: a +300
	 * ppm set ramped from -260 toward +264 over minutes). Frequency
	 * injection must be a step, so drop status and offset with it. */
	memset(&tx, 0, sizeof(tx));
	tx.modes = ADJ_FREQUENCY | ADJ_OFFSET | ADJ_STATUS;
	tx.freq = raw;
	tx.offset = 0;
	tx.status = 0;
	if (adjtimex(&tx) < 0) {
		perror("adjtimex");
		exit(1);
	}
}

int main(int argc, char **argv)
{
	if (argc >= 2 && !strcmp(argv[1], "get")) {
		long f = get_freq();
		printf("%ld %.3f\n", f, f / 65536.0);
		return 0;
	}
	if (argc >= 3 && !strcmp(argv[1], "add")) {
		long old = get_freq();
		long raw = old + (long)(atof(argv[2]) * 65536.0);
		set_freq(raw);
		printf("old %ld new %ld\n", old, get_freq());
		return 0;
	}
	if (argc >= 3 && !strcmp(argv[1], "setraw")) {
		set_freq(atol(argv[2]));
		printf("now %ld\n", get_freq());
		return 0;
	}
	fprintf(stderr, "usage: freqadj get | add <ppm> | setraw <raw>\n");
	return 2;
}
