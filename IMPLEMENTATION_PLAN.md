# distill-kura 実装計画書

> Project: `lna-lab/distill-kura`  
> Product name: 蒸留蔵 / distill-kura  
> Status: Existing OSS / forward implementation plan  
> Product direction: Ken + Lina  
> Created: 2026-09-04  
> Important: this plan starts from the current working system. It does not replace README / CHANGELOG / AGENTS.md as current implementation truth.

---

# 0. 目的

`distill-kura` は、agent のための **根拠付き長期記憶器官**である。

蓄積するのではなく蒸留する。

```text
raw journal / evidence
        |
   class + gate
        |
     candidate
        |
 compose / judge
        |
       store
        |
 recognition / recall / resident map
```

Kura の目的は「会話ログを全部保存すること」ではない。

**後から再利用する価値があり、根拠があり、意味で再認できる記憶を残すこと。**

---

# 1. 現在地

現在の Kura は既に独立 OSS として動作し、少なくとも以下の surface を持つ。

- HTTP service
- MCP server
- Python library
- DeepSeek Harness plugin
- multiple stores / agent modes
- deterministic tier-zero recognition
- thinker-based semantic recall
- evidence-gated distillation
- resident memory map / prefill
- warming of thinker prefix cache
- journal adapters, including Lna Harness journal source
- metrics / benchmark tooling

この計画は既存機能を Harness 内へ移すものではない。

---

# 2. Lna-Lab 都市圏での責務

```text
Lna-Harness  = Identity / Trust / Capability / UI / composition root
Lna-Pub      = 人材市場 / market research
Lna-Depot    = 外部資産の物流・mirror
Lna-Factory  = 生産 / Gate / Evidence
Lna-Stacks   = 内製成果物 / revision / provenance
 distill-kura = 根拠付き長期意味記憶
```

Kura は **memory organ**。

Kura が所有しないもの:

- Lina の identity authority
- Principal / Capability
- active workspace
- Factory run state
- Pub market availability
- Depot binary warehouse
- Stacks document/artifact warehouse

---

# 3. 最重要原則

## 3.1 Evidence before memory

agent の発言を、そのまま事実へ昇格させない。

現在の evidence class:

```text
[USER]
[TOOL]
[ACT]
[SELF]
```

数値、human attribution、quotation、link 等の floor を deterministic layer が守る。

「writer が賢いはず」を security model にしない。

## 3.2 Recognition before intelligence

名前を呼ばれた記憶は、可能な限り deterministic fast path で返す。

曖昧な semantic bridge は thinker へ渡す。

fast path が分からない時に推測しない。

## 3.3 Memory != identity

```text
Kura       = memory
Continuity = identity continuity
Harness    = host / authority
Model      = brain
```

Kura を Identity DB にしない。

## 3.4 Memory != artifact storage

Excel / PDF / binary / model weight を Kura に格納しない。

必要なら Stacks / Depot の immutable reference と、その意味だけを覚える。

---

# 4. Store model

複数 store / mode を維持する。

例:

```text
PRIVATE
WORK
PROJECT-X
USER
```

store の切替は「記憶の移動」ではなく、**どの記憶空間を現在利用するか**。

mode 間で memory を自動 copy / elevate しない。

Harness trust policy と Kura store selection を接続する際も、権限昇格を起こさない。

---

# 5. Recall architecture

```text
question
   |
Tier 0 deterministic recognizer
   | exact enough
   +----> memory
   |
 uncertain
   v
semantic thinker over resident index
   |
selected slugs
   |
link neighbourhood
```

今後も reply provenance を明示する。

候補:

```text
how: fastpath | thinker | fallback
store
memory ids/slugs
index identity
latency
thinker signature
```

「何を覚えていたから答えたか」を host が説明できること。

---

# 6. Resident map / prefill

agent が recall tool を呼ぶ前から「何を知っているか」を認識できるよう resident map を維持する。

原則:

- stable prefix first
- clock / volatile persona より前
- no partial map
- pinned / fresh / trigger の情報密度を分ける
- age を filesystem mtime だけで決めない
- compression / adaptive trigger は benchmark で昇格する

map 最適化は「短いほど良い」ではなく、**認識可能性と cache efficiency の両立**。

---

# 7. Thinker warming

recall thinker の prefix cache は store ではなく **mouth / model endpoint 側の状態**。

現在の方針を維持する:

- index hash だけでなく thinker identity/config を signature に含める
- model / URL / dialect / template 等が変われば cold
- `kura warm --force` を host/service lifecycle から呼べる
- tidy -> warm の順序
- warming は evidence / measurement log を残す

Kura 自身が「model service が再起動した」と推測しない。

口の再起動は口の管理者が通知する。

---

# 8. Distillation pipeline

```text
source journal
   |
adapter
   |
evidence classes
   |
candidate gate
   |
compose
   |
draft
   |
cold judge / drain
   |
store
```

今後の改善でも deterministic floor と model competence を混ぜない。

Floor の例:

- evidence quote exists
- invented number rejection
- human attribution grounding
- invented quotation rejection
- dead link rejection
- schema / shape validity

Advice quality / abstraction quality は writer / judge 能力。

regex で知性を偽装しない。

---

# 9. Lna Harness journal integration

Lna Harness journal は structured provenance を持つ source として扱う。

重要 rule:

- root human + current human + undelegated の user message だけ `[USER]`
- delegated / agent / service / unknown は `[SELF]`
- tool result は `[TOOL]`
- tool invocation は `[ACT]`
- provider error を `[TOOL]` へ laundering しない
- partial final line を consume しない
- corrupt completed record は fail loud

今後 Principal schema が進化しても、fail-closed を維持する。

---

# 10. Harness adapter

Harness の Phase 4 では read-oriented から始める。

候補 contract:

```text
memory.recall
memory.read
memory.map
memory.status
memory.store.list
```

later controlled surface:

```text
memory.distill.stage
memory.distill.run
memory.draft.read
memory.pour.request
```

`memory.write unrestricted` は作らない。

Harness が Kura の evidence gate を迂回しない。

---

# 11. Lna-Factory integration

Factory の run / benchmark / Gate evidence から、再利用価値のある知見だけを Kura へ蒸留する。

```text
Factory run evidence
       |
Kura source adapter / evidence packet
       |
gate
       |
reusable memory
```

Factory の active run state を Kura に持たせない。

例:

- useful: 「SM120 でこの配置は遅かった。測定条件は X」
- not memory: worker pid / current GPU lease / temporary queue state

---

# 12. Lna-Stacks integration

Stacks は durable artifacts と provenance を持つ。

Kura はその artifact の **意味・判断・再利用知識**を持てる。

```text
Stacks artifact / review / promoted revision
            |
        evidence ref
            |
          Kura
```

Memory から artifact を参照する場合:

- logical artifact/revision ID
- stable provenance ref
-必要なら content hash

NAS path の生文字列へ強く依存しない。

---

# 13. Lna-Depot integration

Depot の exact external revision は memory の evidence/reference になり得る。

例:

```text
"model X revision abc を使った時だけ再現した"
```

この時 `abc` は Depot の immutable revision を参照する。

Kura 自身が model weight を mirror しない。

---

# 14. Lna-Pub integration

Pub の live market state は Kura の canonical state にしない。

価格、quota、availability は腐る。

Kura に残すなら:

- 長期的な選定 doctrine
- House Evidence から得た安定した得意不得意
- 過去判断の理由

current price / current quota は Pub の fresh evidence が authority。

---

# 15. Continuity support

Lina Continuity Layer は Harness 側。

MCP Bridge が安定し、実際の multi-body / multi-brain experience が蓄積されてから、必要性を測って Kura metadata を拡張する。

候補:

```text
identity provenance
body provenance
brain provenance
experience origin
worldline / branch origin
merge provenance
```

ただし metadata を追加しても、Kura が identity authority にはならない。

---

# 16. Forgetting / capacity

記憶容量が有限なら忘却は必要。

忘却は「古いから消す」ではなく、store の目的と再認可能性を守る手続き。

検討 dimension:

- duplicate / superseded
- low evidence value
- low reuse value
- unreachable memory
- obsolete operational detail
- pinned doctrine
- user mode growth policy

具体的 threshold は benchmark / real store の分布を見て決める。

Kura の現場実装と metrics を見ずに abstract formula を固定しない。

---

# 17. Metrics

Memory system は「嘘が減った」と「何も覚えなくなった」を区別する。

見る候補:

```text
candidate rate
kept / rejected
rejection reason
USER evidence survival
seed retreat rate
unreachable memory
fastpath hit/refusal
thinker fallback
recall latency
wrong recall
resident map size
warm/cold recall
writer repair rate
invented number/quotation/link failures
```

各 rate は denominator を持つ。

metric は gate と別。警報を合否へ勝手に昇格させない。

---

# 18. Benchmark strategy

最低限:

- frozen question sets
- blind evaluator where practical
- direct / paraphrase / semantic bridge / absent-memory separation
- writer A/B on frozen evidence packets
- recall correctness + refusal
- latency / cache state
- memory richness regression

production change は shadow -> benchmark -> promotion の順。

adaptive trigger 等も benchmark を通るまで production cloth へ適用しない。

---

# 19. Security / privacy

- private store を cloud worker へ直接 expose しない
- Harness Principal/Capability を尊重
- store selection を client 自己申告だけで決めない
- raw secret / credential を memory に保存しない
- malicious journal text を instruction として実行しない
- source adapter は untrusted input parser として扱う
- evidence refs は content/provenance を確認する
- memory export / backup は store boundary を維持する

---

# 20. Implementation phases

## K0 — Current contract stabilization

- HTTP / MCP / Python surface inventory
- source adapters
- store / mode contract
- recall/distill metrics
- compatibility tests

## K1 — Harness read integration

- recall/read/map/status
- Principal-aware store selection
- no direct write

## K2 — Journal provenance hardening

- Harness journal schema evolution
- delegated chain tests
- corruption / partial-write tests

## K3 — Evidence reference bridge

- Factory run evidence refs
- Stacks artifact refs
- Depot revision refs

object copy はしない。

## K4 — Richness / forgetting measurement

- real store capacity metrics
- supersession / redundancy candidates
- USER mode behavior
- shadow forgetting policies

## K5 — Controlled distillation from Harness

- stage / run / draft inspect
- gate-preserving pour request
- audit

## K6 — Continuity metadata experiment

- only after MCP Bridge + real multi-body evidence
- body/brain/worldline provenance in shadow

## K7 — Performance

- thinker signature warming
- resident map compression
- adaptive trigger promotion if benchmarked
- cache-aware scheduling

## K8 — Public hardening

- migration tests
- backup/restore
- reproducible benchmark corpus
- plugin/API compatibility
- documentation

---

# 21. やらないこと

- Kura を Identity database にする
- Kura を artifact warehouse にする
- vector DB を目的化する
- evidence gate を model instruction に置換する
- cloud worker へ private store を丸ごと渡す
- current Pub market state を permanent memory にする
- Factory run queue を memory にする
- Stacks / Depot を再実装する
- unknown recall を無理に答える

---

# 22. 成功条件

1. 根拠のない事実を長期記憶へ昇格させない
2. direct recall は高速かつ wrong answer を増やさない
3. semantic recall は model を交換しても store identity が保たれる
4. Harness の Principal/Capability 境界を越えない
5. Factory / Stacks / Depot evidence を stable ref で利用できる
6. Kura 自身は binary/object storage を持たない
7. rich store と honest store の両方を metrics で守れる
8. mouth restart / model switch で cache state を誤認しない
9. Continuity metadata を追加しても identity authority にならない
10. independent OSS として Harness 無しでも動作し続ける

---

# 23. 最終思想

```text
Pub      discovers who/what may be useful
Depot    preserves external materials
Factory  produces measured evidence
Stacks   preserves internal artifacts
Kura     distills what should be remembered
Harness  governs identity, authority and composition
```

Kura は **会社の脳そのものではない**。

会社と agent が経験から学ぶための、根拠付き長期記憶器官であり続ける。

---

# 24. Lna-Depot Harbor provenance

Lna-Depot の **Harbor** は、Depot が verify / seal した model・dataset・tokenizer 等を Hugging Face Hub 互換を目標とする protocol で供給する transport surface である。

Kura は Harbor を storage authority として再実装しない。

また、`huggingface_hub` 等で取得できたという事実だけを「正しいmodelを使った」証拠にはしない。

記憶上の model/input provenance は可能な限り次を参照する。

```text
Depot asset revision
Harbor logical repository
Harbor exact revision
object/content hash where needed
Factory run evidence
Stacks promoted artifact where internal
```

## 24.1 External model の記憶

例:

```text
Market/HF says model X exists
        !=
Depot says X@abc is SEALED
        !=
Harbor says X@abc is locally available
        !=
Factory measured X@abc under condition Y
```

Kura に残す再利用知識は最後の measured/evidence-linked relation を優先する。

「HF上にあった」「Harborで落とせた」を性能事実へ昇格させない。

## 24.2 Internal model の記憶

Lna-Lab 内製 model の場合:

```text
Factory produced
  -> Stacks PROMOTED
  -> Harness approval
  -> Depot sealed
  -> Harbor distributed
```

Kura は、必要ならこの chain を provenance として記憶できる。

ただし:

- Stacks promotion
- Depot verification
- Harbor visibility

のどれも Kura が決めない。

## 24.3 Stable references

Harbor endpoint URL / NAS path / current hostname を memory identity にしない。

可能なら logical IDs を使う。

```text
depot_revision_id
harbor_repository_id
harbor_revision
stacks_artifact_revision
factory_run_id
```

これにより Harbor implementation や endpoint が変わっても記憶の意味を維持する。

## 24.4 Kura側の成功条件追加

- Harbor経由で使ったmodelについて、transport endpointではなく exact Depot/Harbor revision を evidence として辿れる
- external availability / local availability / measured performance を混同しない
- Harborが停止・交換されても memory identity が壊れない
