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

## 词典口径 v3(20260826-01 rework-1,创始人 2026-08-27 拍板)

- **canonical = 业务惯用写法**(合学/功能合学/维也纳合学/颌平面/合位…);`𬌗`(U+2C317)
  系正字全部退役入 variants/note 备查——CJK Ext F 生僻字,手机端有豆腐块渲染风险,
  清洗稿与全部下游产物不得出现(实测 0,含 step2 卡片/step4 chunks/step5 题库讲义)。
- corpus-hub 侧 `config/dict/he-xue.yaml` 已同步 v3(其仓 92b792e);其 `general.yaml`
  的 𬌗位/𬌗位关系 canonical 为服务妙记源的独立副本,有意未同步,与本仓无共享单源。
- `corpus/migrate_canonical.py`:一次性口径迁移工具(词典 canonical 变更后,step2/step5
  LLM 产物按 dict 中含退役字的 variant→canonical 最长优先机械替换,quote 定位不变式保持;
  step1/1b/4 由管道重跑再生)。幂等:再跑替换数=0。

## 提交留痕

- `3acd23f`(2026-08-27 21:47,提交信息"auto: 每日自动备份")实际吸收了 20260826-01 rework-1
  的修复文件:config/seed_terms.yaml(v3)、config/step1_guards.yaml、corpus/migrate_canonical.py、
  corpus/selftest.sh(v3 检查项)、corpus/step1b_hepingmian.py——内容经核为交付所需且完整,
  此后工作区 clean,不做 revert;信息与内容不符系自动备份吸改所致,特此留痕
  (与 88392cf 同类教训:提交前逐文件核对,勿信既有提交信息)。
