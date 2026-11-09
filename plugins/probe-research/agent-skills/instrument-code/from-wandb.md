# Switch a script from W&B to Probe

Read this when a training, eval or sweep script logs to W&B (`import wandb`) and the person wants it to log to Probe
instead. The runs W&B already holds come over with the W&B import, which checks each copied run against W&B point
for point; this page moves the CODE.

## THE ORDER

1. **Keep both for one run.** Add the Probe calls beside the W&B ones, `probe.init` AFTER `wandb.init`: Probe then
   records the active `wandb.run`'s id on its run, so an import later fills that same run instead of making a second
   one. Run it once.
2. **Check parity.** Import that W&B project: `probe import review --wandb-project <entity/project> --json`, then,
   once the person says yes to its consent lines, `probe import start <id> --directed`. When `probe import status`
   says it is done, read its parity line: `identical`, or `only expected differences` naming why. A run that
   `differs` is named, its series listed in `imports/wandb-parity-<date>.md` on the project: fix the code first.
3. **Remove W&B.** Delete every `wandb` call and the import, run once more, then verify at the destination
   (`probe run get <id>`, `probe metrics plot <id>`): the same keys as W&B's, at the same steps.

Show the person the diff before each run. Never touch a W&B key, the `.netrc` or `WANDB_*` variables.

## THE CALL MAP

| W&B | Probe |
|---|---|
| `wandb.init(project=, name=, config=, tags=, notes=, group=)` | `probe.init(project=<slug>, name=, config=, tags=, notes=)`; a group is a tag (`group:<g>`) |
| `wandb.config.update(d)`, `wandb.config.x = v` | `probe.update_config(d)` |
| `wandb.log(d, step=i)` | `probe.log(d, step=i)`: the same keys, the same step. A key Probe gets without a step makes no curve |
| `wandb.log(d, commit=False)` then a later commit | one `probe.log` per call, each with that step: no commit to wait for |
| `wandb.define_metric("val/*", step_metric="epoch")` | log `val/*` at the global step and `epoch` as its own key; charts pick their x axis |
| `wandb.summary["best"] = x`, `run.summary.update(d)` | `probe.finish(summary_metrics={"best": x})` |
| `wandb.save("*.pt")`, files written in the run's folder | nothing: the run's folder is captured when it ends (`SKILL.md`, FILES AND LOGS) |
| `wandb.log_artifact(art)`, `art.add_file(p)` | `probe.log_artifact("<name>/<file>", path=p)`, one per file; a `.pt`/`.ckpt`/`.safetensors` is filed `checkpoint` |
| `run.use_artifact("ds:v3")` | nothing when the script opens the file (`SKILL.md`, WHAT THE RUN READ); else `run.use_dataset("dataset:NAME@V")` |
| `wandb.Image(x)`, `wandb.Video`, `wandb.Audio` in `wandb.log` | save the file, then `probe.log_artifact(f"media/<key>/{step}.png", path=p, step_index=step)` |
| `wandb.Table(...)` | write it as CSV or parquet, then `probe.log_artifact(f"tables/<key>/{step}.csv", path=p, step_index=step)` |
| `wandb.alert(title=, text=)` | no twin for a message on demand; `probe.expect({"val/acc": (0.5, 1.0)})` emails when a metric leaves its range |
| `wandb.watch(model)` | **no twin**: Probe keeps no gradient or parameter histograms. Log what you read from them as metrics (`probe.log({"grad/norm": n}, step=i)`) |
| `wandb.sweep` / `wandb.agent` | **no sweep controller**: keep your own driver (a loop, Optuna, Ray Tune) and file every trial in one experiment, `probe.init(project=, experiment="<sweep>", config=trial)` |
| `wandb.init(resume=...)` | `SKILL.md`, RELAUNCHING A RUN |
| `wandb.finish()` | `probe.finish()` |
| `mode="offline"` | nothing: Probe queues and sends when it can |

## FRAMEWORKS

- **HF `Trainer`**: `report_to="wandb"` becomes `report_to="none"` plus a `TrainerCallback` whose `on_log(args,
  state, control, logs)` calls `probe.log(..., step=state.global_step)`. W&B's callback renamed every key: `eval_<k>`
  to `eval/<k>`, `test_<k>` to `test/<k>`, anything else to `train/<k>` (`epoch` too): rename the same way, or the
  curves come back under other names (`train/global_step` was W&B's own x axis: Probe's step is that already).
  `probe.init(config=args.to_dict())` before `trainer.train()`, `probe.finish()` after.
- **Lightning**: `WandbLogger` becomes a `lightning.pytorch.loggers.Logger` subclass (`name`, `version`,
  `log_hyperparams(params)` calling `probe.update_config(...)`, `log_metrics(metrics, step)` calling
  `probe.log(metrics, step=step)`); the `self.log` names stay. `trainer/global_step` was W&B's own x axis: Probe's
  step is that already. `probe.init` before `trainer.fit`, `probe.finish()` after.
- Anything else with a W&B callback: find where it calls `wandb.log` and call `probe.log` there instead.

Keep the metric NAMES W&B had: dashboards, parity and the person's memory all use them.
