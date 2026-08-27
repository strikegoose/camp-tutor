# camp-tutor 仓库规则(训练营 AI 助教·内容管道)

## 定位

学科无关的离线内容加工管道。本仓库只产 `corpus/` 管道与 `config/` 数据;审核台/H5 在后续指令单。

## 铁律

- **学科无关**:课程名单/术语/框架名/营期窗口等学科数据一律进 `config/`,代码零硬编码
- **只读数据源**:`~/NAS-数据仓/转写语料/`、`~/NAS-视频/`、富兰克林副本库(只读查询,禁任何写);产出全写本仓库 `data/`
- `.env`(600)不进 git;`data/`、`logs/` 不进 git
- LLM 调用一律经 `corpus/lib/llm.py`(URL/MODEL/KEY 走环境变量;内容寻址缓存,重跑零成本);禁止裸写 HTTP 调用
- 每部件:幂等 + 版本化(`data/runs/<ts>/`)+ 日志(`logs/`,输入输出摘要+耗时)+ token 台账(`common.record_tokens`)
- **任何部件失败路径必须 `common.notify(...)`(try 包裹、不阻塞主流程、落 logs/notify.log)**,顶层 `__main__` 已有兜底,部件内部可恢复失败也要 notify
- 运行目录:步骤内用 `common.get_run_dir()`;`run_all.sh` 负责创建新 run 并导出 `CAMP_TUTOR_RUN_DIR`
- Python 解释器:`.venv/bin/python`(pyyaml/pypinyin/pillow 已装)

## 输入契约

- `config/courses.yaml`:44 节名单(canonical_id/camp/dir_name/title/instructor/case_series)+ 营期窗口
- `config/seed_terms.yaml` / `config/framework.yaml`
- `config/step1_guards.yaml`:部件1 替换守卫(block_prev/block_next 保护合法词、「X类」序数语境规则)
- `config/step1_exclude.yaml`:部件1 人工终审(exclude 剔除有害裁决对 / include 补入未裁决术语对)
- `data/latest/step0/courses_master.json`:canonical 主表(含 transcript_path/video_path/instructor 等)

## 转写稿格式

见 `corpus/lib/transcript.py`:`parse()` → Transcript(header, keywords, blocks[Speaker/start_sec/text]);清洗稿必须保持同格式(`serialize()`)。

## T2 公共组件(20260826-03 语料库平台化)

逐字稿解析(`corpus/lib/transcript.py`)、术语替换引擎(step1 的 RuleSet)、标题匹配(step0 的
normalize/similarity/assign_1to1)已抽取至 corpus-hub(`~/Claude/projects/corpus-hub/`,
可用环境变量 CORPUS_HUB 覆盖)作为公司级公共组件;本仓库对应文件为调用 shim/导入,行为不变。
落地史:2026-08-27 03:40 -03 首次 shim 化 → 04:2x 被本仓库会话 `git add -A` 误扫入提交 88392cf
→ 04:45 还原全量实现(25e93db) → 05:5x -03 按指令单正式重落地,`bash corpus/selftest.sh`
复跑 44/44 全绿;shim 化前后 step1 重跑输出与在途批次逐字节一致(差异仅 step1b 和平面产物)。
注意:`git add -A` 前请确认这三个文件的 shim 状态是要保留的交付态,勿再误扫误标。
