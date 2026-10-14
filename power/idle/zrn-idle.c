// SPDX-License-Identifier: GPL-2.0
/*
 * zrn-idle: CPU idle for the Exynos 9810 (Galaxy S9) under mainline.
 *
 * Without it the cores only ever pause in WFI: powered, clocked, caches on.
 * The firmware does offer per-core power-down through PSCI CPU_SUSPEND
 * (state 0x10000, as the stock device tree has it), but on this SoC the PMU
 * is helped by a small "flexible PMU" controller that has to be told first.
 * Samsung's kernel (exynos-powermode.c, cal-if pmucal_cpu.c) does, around
 * every core power-down:
 *
 *   before   CPU_INFORM[core] = C2 (1)            PMU 0x14060860 + 4 * core
 *            GRP2_INTR_BID_ENABLE |= bit           0x14070200
 *            if GRP1 pending, GRP1_INTR_BID_CLEAR  0x14070108 / 0x1407010c
 *            PMU_EVENT_INTERRUPT_ENABLE_GRP2 |= bit  PMU 0x14067f08
 *   after    CPU_INFORM[core] = 0, the three again with the bit cleared
 *            (GRP2 pending cleared instead: 0x14070208 / 0x1407020c)
 *
 * where core is cluster * 4 + cpu (little cluster 0; see zrn_core()) and bit is core's
 * interrupt line into the controller: 4..7 for the little cores, 0..3 for
 * the big ones. Without that, a core that powers down is not woken again,
 * which is also why an offlined core never comes back on this kernel.
 *
 * This registers a cpuidle driver per cluster: WFI, and the power-down state
 * with the stock latencies. psci_cpu_suspend_enter() is not exported, so its
 * address is passed in from /proc/kallsyms:
 *
 *   insmod zrn-cpuidle.ko suspend_enter=0x$(awk '$3=="psci_cpu_suspend_enter"{print $1}' /proc/kallsyms) cpus=0xff
 *
 * cpus is a mask of the CPUs to drive (default 0xff); the others keep WFI.
 */
#include <linux/cpu.h>
#include <linux/clocksource.h>
#include <linux/interrupt.h>
#include <linux/irq.h>
#include <linux/iopoll.h>
#include <linux/of.h>
#include <linux/of_irq.h>
#include <linux/cpu_pm.h>
#include <linux/cpuidle.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/arm-smccc.h>
#include <linux/tick.h>
#include <asm/cputype.h>
#include <asm/memory.h>

#define PMU_BASE		0x14060000
#define PMU_CPU_INFORM(c)	(0x0860 + 4 * (c))
#define PMU_EVENT_INT_EN_GRP2	0x7f08
#define BID_BASE		0x14070000
#define GRP1_PEND		0x0108
#define GRP1_CLEAR		0x010c
#define GRP2_ENABLE		0x0200
#define GRP2_PEND		0x0208
#define GRP2_CLEAR		0x020c
#define INFORM_C2		1
#define PSCI_CORE_POWERDOWN	0x10000

static unsigned long suspend_fn, resume_fn;
module_param_named(suspend_enter, suspend_fn, ulong, 0444);
MODULE_PARM_DESC(suspend_enter, "address of cpu_suspend (from /proc/kallsyms)");
module_param_named(resume, resume_fn, ulong, 0444);
MODULE_PARM_DESC(resume, "address of cpu_resume (from /proc/kallsyms)");
static unsigned int cpus = 0xff;
module_param(cpus, uint, 0444);
MODULE_PARM_DESC(cpus, "mask of the CPUs to give the power-down state");

static int (*cpu_suspend_p)(unsigned long arg, int (*fn)(unsigned long));
static unsigned long cpu_resume_pa;
static void __iomem *pmu, *bid;

/*
 * The finisher: one SMC, the function id Samsung's firmware implements
 * (from the stock device tree's psci node), with the state and the physical
 * address cpu_resume runs at. It must not touch the stack: cpu_suspend has
 * already saved the register frame it will be resumed into.
 */
static int zrn_suspend_finisher(unsigned long state)
{
	struct arm_smccc_res res;

	arm_smccc_smc(0xC4000001, state, cpu_resume_pa, 0, 0, 0, 0, 0, &res);
	return (int)res.a0;
}
static DEFINE_RAW_SPINLOCK(pmu_lock);

/*
 * The PMU's core number, as Samsung's linear_phycpu(): cluster * 4 + core.
 * The little Cortex-A55s report a multithreading-style MPIDR (MT set, core
 * in Aff1, cluster in Aff2); the big cores the plain one (core in Aff0).
 */
static unsigned int zrn_core(u64 mpidr)
{
	int lvl = (mpidr & MPIDR_MT_BITMASK) ? 1 : 0;

	return (MPIDR_AFFINITY_LEVEL(mpidr, (lvl + 1)) << 2) | MPIDR_AFFINITY_LEVEL(mpidr, lvl);
}

static void rmw(void __iomem *reg, u32 mask, u32 value)
{
	writel_relaxed((readl_relaxed(reg) & ~mask) | value, reg);
}

static void core_off(unsigned int core)
{
	u32 bit = BIT((core + 4) & 7);

	if (WARN_ON_ONCE(core > 7))
		return;
	raw_spin_lock(&pmu_lock);
	writel_relaxed(INFORM_C2, pmu + PMU_CPU_INFORM(core));
	rmw(bid + GRP2_ENABLE, bit, bit);
	if (readl_relaxed(bid + GRP1_PEND) & bit)
		rmw(bid + GRP1_CLEAR, bit, bit);
	rmw(pmu + PMU_EVENT_INT_EN_GRP2, bit, bit);
	raw_spin_unlock(&pmu_lock);
	wmb();
}

static void core_on(unsigned int core)
{
	u32 bit = BIT((core + 4) & 7);

	if (core > 7)
		return;
	raw_spin_lock(&pmu_lock);
	writel_relaxed(0, pmu + PMU_CPU_INFORM(core));
	rmw(bid + GRP2_ENABLE, bit, 0);
	if (readl_relaxed(bid + GRP2_PEND) & bit)
		rmw(bid + GRP2_CLEAR, bit, bit);
	rmw(pmu + PMU_EVENT_INT_EN_GRP2, bit, 0);
	raw_spin_unlock(&pmu_lock);
}

static int enter_wfi(struct cpuidle_device *dev, struct cpuidle_driver *drv, int idx)
{
	dsb(sy);
	wfi();
	return idx;
}

static int enter_powerdown(struct cpuidle_device *dev, struct cpuidle_driver *drv, int idx)
{
	u64 mpidr = read_cpuid_mpidr();
	unsigned int core = zrn_core(mpidr);
	int ret;

	if (core > 7)	/* not a core the PMU knows: stay in WFI */
		return enter_wfi(dev, drv, 0);
	ret = cpu_pm_enter();
	if (ret)
		return -EBUSY;
	core_off(core);
	/*
	 * Not psci_cpu_suspend_enter(): it reads bit 30 of the state word to
	 * tell retention from power-down, and Samsung's 0x10000 has it clear,
	 * so it would pass entry point 0 -- and a core that powers down
	 * resumes through no frame at all (seen: the first IRQ lands on a
	 * corrupted stack). cpu_suspend saves the frame on this stack and our
	 * finisher hands the firmware cpu_resume's physical address, exactly
	 * what Samsung's own idle path does.
	 */
	ret = cpu_suspend_p(PSCI_CORE_POWERDOWN, zrn_suspend_finisher);
	core_on(core);
	cpu_pm_exit();
	return ret ? -EBUSY : idx;
}

static struct cpuidle_driver drivers[2] = {
	{
		.name = "zrn_idle_little",
		/*
		 * No .owner: with one, each registered CPU holds the module and
		 * it can never be unloaded. cpuidle_unregister() waits for the
		 * CPUs to leave our states before the module goes.
		 */
		.states = {
			{ .name = "WFI", .desc = "wait for interrupt", .enter = enter_wfi,
			  .exit_latency = 1, .target_residency = 1 },
			{ .name = "C2", .desc = "core power down", .enter = enter_powerdown,
			  .flags = CPUIDLE_FLAG_TIMER_STOP,
			  .exit_latency = 160, .target_residency = 2000 },
		},
		.state_count = 2,
	},
	{
		.name = "zrn_idle_big",
		.states = {
			{ .name = "WFI", .desc = "wait for interrupt", .enter = enter_wfi,
			  .exit_latency = 1, .target_residency = 1 },
			{ .name = "C2", .desc = "core power down", .enter = enter_powerdown,
			  .flags = CPUIDLE_FLAG_TIMER_STOP,
			  .exit_latency = 220, .target_residency = 3500 },
		},
		.state_count = 2,
	},
};
static bool registered[2];

static void zrn_broadcast_exit(void);

static void zrn_idle_exit(void)
{
	/* off the idle states first: they are what needs the broadcast */
	for (int i = 0; i < 2; i++) {
		if (registered[i])
			cpuidle_unregister(&drivers[i]);
		kfree(drivers[i].cpumask);
	}
	zrn_broadcast_exit();
	if (bid)
		iounmap(bid);
	if (pmu)
		iounmap(pmu);
}


/*
 * ------------------------------------------------- the MCT's global comparator
 *
 * A core that powers down also stops its arch timer, and this kernel has no
 * hardware broadcast timer: the only one registered is bc_hrtimer, the
 * software fallback, which schedules on a CPU that is itself about to stop
 * ticking. Anything sleeping on a timer on an idled core then never wakes
 * (measured: sshd hung while the phone still answered ping).
 *
 * The Exynos MCT (0x10040000) runs off the oscillator and never stops. Its
 * global comparator 0, GIC SPI 466 (from the stock device tree's mct-map),
 * becomes the broadcast device instead: timers of powered-down cores are
 * served by a G0 interrupt on cpu0, which IPIs the sleeping cores. Samsung's
 * kernel uses the MCT for exactly this.
 *
 * The rate is measured, not assumed: the comparator cycle count comes from
 * delta_ns * rate / NSEC_PER_SEC, and a wrong rate moves every broadcast
 * wakeup.
 */
#define MCT_PHYS		0x10040000UL
#define MCT_G_CNT_L		0x100
#define MCT_G_CNT_U		0x104
#define MCT_G_CNT_WSTAT		0x110
#define MCT_G_COMP0_L		0x200
#define MCT_G_COMP0_U		0x204
#define MCT_G_TCON		0x240
#define MCT_G_INT_CSTAT		0x244
#define MCT_G_INT_ENB		0x248
#define MCT_G_WSTAT		0x24c
#define MCT_TCON_START		(1U << 8)
#define MCT_TCON_COMP0_EN	(1U << 0)
#define MCT_G0_SPI		466

static void __iomem *mct;
static struct clock_event_device zrn_bc;
static unsigned int zrn_bc_irq = ~0U;
static bool zrn_bc_live;

static void mct_wr(u32 v, u32 off, u32 wstat_bit)
{
	u32 stat;

	writel_relaxed(v, mct + off);
	if (readl_poll_timeout_atomic(mct + MCT_G_WSTAT, stat,
				      stat & wstat_bit, 1, 100))
		pr_warn("zrn-idle: MCT write %#x did not land\n", off);
	writel_relaxed(wstat_bit, mct + MCT_G_WSTAT);	/* the status clears on a 1 */
}

static u64 mct_count(void)
{
	u32 hi, hi2, lo;

	hi = readl_relaxed(mct + MCT_G_CNT_U);
	do {
		lo = readl_relaxed(mct + MCT_G_CNT_L);
		hi2 = readl_relaxed(mct + MCT_G_CNT_U);
		if (hi == hi2)
			return ((u64)hi << 32) | lo;
		hi = hi2;
	} while (1);
}

static unsigned long mct_measure_rate(void)
{
	u64 a, b, ns;
	ktime_t t0 = ktime_get();

	a = mct_count();
	while (ktime_to_ns(ktime_sub(ktime_get(), t0)) < 200 * NSEC_PER_MSEC)
		cpu_relax();
	ns = ktime_to_ns(ktime_sub(ktime_get(), t0));
	b = mct_count();
	return ns ? (unsigned long)div64_u64((b - a) * NSEC_PER_SEC, ns) : 0;
}

static void mct_comp0_stop(void)
{
	u32 tcon = readl_relaxed(mct + MCT_G_TCON);

	writel_relaxed(0, mct + MCT_G_INT_ENB);
	tcon &= ~MCT_TCON_COMP0_EN;
	mct_wr(tcon, MCT_G_TCON, 1U << 16);
}

static int zrn_bc_set_next(unsigned long cycles, struct clock_event_device *evt)
{
	u64 comp = mct_count() + cycles;
	u32 tcon = readl_relaxed(mct + MCT_G_TCON);

	writel_relaxed(1, mct + MCT_G_INT_CSTAT);
	tcon &= ~MCT_TCON_COMP0_EN;
	mct_wr(tcon, MCT_G_TCON, 1U << 16);
	mct_wr((u32)comp, MCT_G_COMP0_L, 1U << 0);
	mct_wr((u32)(comp >> 32), MCT_G_COMP0_U, 1U << 1);
	writel_relaxed(1, mct + MCT_G_INT_ENB);
	tcon |= MCT_TCON_COMP0_EN;
	mct_wr(tcon, MCT_G_TCON, 1U << 16);
	return 0;
}

static int zrn_bc_stop(struct clock_event_device *evt)
{
	if (zrn_bc_live)
		mct_comp0_stop();
	return 0;
}

static irqreturn_t zrn_bc_isr(int irq, void *dev)
{
	struct clock_event_device *evt = dev;

	/* only comparator 0's flag is ours; a stray line (wrong SPI guess)
	 * shows up as spurious instead of waking cores at random */
	if (!(readl_relaxed(mct + MCT_G_INT_CSTAT) & 1))
		return IRQ_NONE;
	writel_relaxed(1, mct + MCT_G_INT_CSTAT);
	evt->event_handler(evt);
	return IRQ_HANDLED;
}

/*
 * Register the comparator as a global oneshot clockevent. Rating 300: above
 * bc_hrtimer (0), so the framework installs it as the broadcast device, and
 * below the arch timers (~400), so the per-CPU tick devices are untouched.
 */
static int __init zrn_broadcast_init(void)
{
	struct device_node *gic;
	struct irq_fwspec spec = { };
	unsigned long rate;
	u32 tcon;
	int virq, ret;

	mct = ioremap(MCT_PHYS, 0x800);
	if (!mct)
		return -ENOMEM;

	tcon = readl_relaxed(mct + MCT_G_TCON);
	if (!(tcon & MCT_TCON_START)) {	/* the counter is not even running */
		mct_wr(tcon | MCT_TCON_START, MCT_G_TCON, 1U << 16);
	}
	rate = mct_measure_rate();
	if (rate < 1000000) {
		pr_err("zrn-idle: MCT not ticking (rate %lu Hz): no broadcast, no C2\n", rate);
		ret = -ENODEV;
		goto err;
	}
	pr_info("zrn-idle: MCT G0 at %lu Hz is the broadcast timer\n", rate);

	gic = of_find_compatible_node(NULL, NULL, "arm,gic-400");
	if (!gic) {
		ret = -ENODEV;
		goto err;
	}
	spec.fwnode = of_fwnode_handle(gic);
	spec.param_count = 3;
	spec.param[0] = 0;			/* GIC_SPI */
	spec.param[1] = MCT_G0_SPI;
	spec.param[2] = IRQ_TYPE_LEVEL_HIGH;
	of_node_put(gic);
	virq = irq_create_fwspec_mapping(&spec);
	if (virq <= 0) {
		ret = -ENOENT;
		goto err;
	}

	zrn_bc = (struct clock_event_device){
		.name = "zrn-mct-g0",
		.features = CLOCK_EVT_FEAT_ONESHOT,
		.rating = 300,
		.irq = virq,
		.set_next_event = zrn_bc_set_next,
		.set_state_shutdown = zrn_bc_stop,
		.set_state_oneshot = zrn_bc_stop,
		.set_state_oneshot_stopped = zrn_bc_stop,
		.tick_resume = zrn_bc_stop,
		.cpumask = cpumask_of(0),
		.owner = THIS_MODULE,
	};
	ret = request_irq(virq, zrn_bc_isr, IRQF_TIMER | IRQF_IRQPOLL,
			  "zrn-mct-g0", &zrn_bc);
	if (ret)
		goto err_dispose;
	zrn_bc_irq = virq;
	clockevents_config_and_register(&zrn_bc, rate, 0xf, UINT_MAX);
	zrn_bc_live = true;
	return 0;

err_dispose:
	irq_dispose_mapping(virq);
err:
	iounmap(mct);
	mct = NULL;
	return ret;
}

static void zrn_broadcast_exit(void)
{
	/* clockevents cannot be unregistered from a module. While the comparator
	 * is the broadcast timer the framework holds it (dev->owner), so the
	 * module is pinned and this never runs; it only cleans up a device that
	 * registered but was never installed. */
	if (!zrn_bc_live)
		return;
	zrn_bc_live = false;
	free_irq(zrn_bc_irq, &zrn_bc);
	irq_dispose_mapping(zrn_bc_irq);
	iounmap(mct);
	mct = NULL;
}

static int __init zrn_idle_init(void)
{
	int ret = 0;

	if (!suspend_fn) {
		pr_err("zrn-idle: give suspend_enter= (cpu_suspend from /proc/kallsyms)\n");
		return -EINVAL;
	}
	if (!resume_fn) {
		pr_err("zrn-idle: give resume= (cpu_resume from /proc/kallsyms)\n");
		return -EINVAL;
	}

	cpu_suspend_p = (void *)suspend_fn;
	if (resume_fn >= 0xffff000000000000UL)
		cpu_resume_pa = __pa_symbol(resume_fn);
	else
		cpu_resume_pa = resume_fn;

	pr_info("zrn-idle: cpu_suspend=%px, cpu_resume=%px (PA 0x%lx)\n",
		cpu_suspend_p, (void *)resume_fn, cpu_resume_pa);

	ret = zrn_broadcast_init();
	if (ret) {
		pr_err("zrn-idle: no broadcast timer: staying out of the kernel's way\n");
		return ret;
	}
	pmu = ioremap(PMU_BASE, 0x8000);
	bid = ioremap(BID_BASE, 0x1000);
	if (!pmu || !bid) {
		zrn_idle_exit();
		return -ENOMEM;
	}

	for (int i = 0; i < 2; i++) {
		struct cpumask *mask = kzalloc(cpumask_size(), GFP_KERNEL);
		unsigned int cpu;

		if (!mask) {
			ret = -ENOMEM;
			break;
		}
		drivers[i].cpumask = mask;
		/* CPUs 0-3 are the little cluster, 4-7 the big (MPIDR 0x00x, 0x10x) */
		for_each_possible_cpu(cpu) {
			if (cpu / 4 == i && (cpus & BIT(cpu)))
				cpumask_set_cpu(cpu, mask);
		}
		if (cpumask_empty(mask))
			continue;
		ret = cpuidle_register(&drivers[i], NULL);
		if (ret) {
			pr_err("zrn-idle: %s: %d\n", drivers[i].name, ret);
			break;
		}
		registered[i] = true;
		pr_info("zrn-idle: %s on CPUs %*pbl\n", drivers[i].name, cpumask_pr_args(mask));
	}
	if (ret)
		zrn_idle_exit();
	return ret;
}

module_init(zrn_idle_init);
module_exit(zrn_idle_exit);
MODULE_DESCRIPTION("Exynos 9810 per-core power-down idle state with the flexible PMU's hints");
MODULE_LICENSE("GPL");
