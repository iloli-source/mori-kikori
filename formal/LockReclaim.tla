--------------------------- MODULE LockReclaim ---------------------------
(* pid ファイル / mkdir 方式の「起動時ロック + stale 回収」の汎用最小モデル。
   シェルの noclobber pidfile や mkdir + pid ファイルを定数で切り替えて同じモジュールで検査できる。
   別プロジェクトで使っている汎用モデルに、本プロジェクトの旧実装が持っていた
   「pid を書いた直後に自分の pid が残っているか読み直す」手順（RecheckPid）を足したもの。

   ロックは「パス上の1オブジェクト」で、作成だけが原子的（noclobber / mkdir）。
   削除・rename は「いまパスにある物」に作用し、自分が観測した物かどうかを区別できない。
   これが stale 回収の ABA（他人が作り直した新しいロックを消す）の原因になる。

   定数:
     Procs           同時に起動し得るプロセス集合（例 {"a","b"}）
     ReclaimMode     "rm_create"     stale と判断 → rm → 作り直し（修正前の典型）
                     "rename_create" stale と判断 → rename で退避 → 作り直し
                                     （rename 自体は原子的だが観測済みの物とは限らない）
                     "kernel"        flock / lockf 等のカーネルロック。保持者が死ぬと
                                     OS が解放するので stale 回収という手順自体が無い
     PidWrite        "atomic"        作成と pid 書き込みが1操作（noclobber echo $$ >）
                     "after_create"  mkdir の後で pid ファイルを書く（2操作）
     EmptyPidIsStale pid 未記入（空/無し）のロックを stale とみなすか
     InitialStale    開始時点で、死んだプロセスの残したロックがあるか
     RecheckPid      TRUE = pid 書き込み（パスに在るディレクトリへ無条件に書く）の後で読み直し、
                     自分の pid でなければ降りる。終了時の片付けも自分の pid のときだけ行う

   検査する性質: MutualExclusion（ロックを取れたと信じて本処理に入るのは高々1つ）。
   モデル化しないもの: pid 再利用（kill -0 が別プロセスに当たり「実行中」と誤判定して
   永久に起動できない活性の問題）。kernel 方式なら pid を見ないので発生しない。 *)
EXTENDS Naturals, FiniteSets

CONSTANTS Procs, ReclaimMode, PidWrite, EmptyPidIsStale, InitialStale, RecheckPid

ASSUME ReclaimMode \in {"rm_create", "rename_create", "kernel"}
ASSUME PidWrite \in {"atomic", "after_create"}
ASSUME EmptyPidIsStale \in BOOLEAN /\ InitialStale \in BOOLEAN /\ RecheckPid \in BOOLEAN

VARIABLES pathCreator,  \* ロックパスにある物の作成者: "none" | "dead" | p
          pathPid,      \* その中の pid: "none" | "empty" | "dead" | p
          kernelHolder, \* kernel 方式の保持者: "none" | p
          pc            \* p ごとの位置
vars == <<pathCreator, pathPid, kernelHolder, pc>>

PcValues == {"start", "inspect", "reclaim", "recreate", "writepid", "recheck", "cs", "done"}

TypeOK ==
  /\ pathCreator \in Procs \cup {"none", "dead"}
  /\ pathPid \in Procs \cup {"none", "empty", "dead"}
  /\ kernelHolder \in Procs \cup {"none"}
  /\ pc \in [Procs -> PcValues]

Init ==
  /\ pathCreator = IF InitialStale /\ ReclaimMode # "kernel" THEN "dead" ELSE "none"
  /\ pathPid = IF InitialStale /\ ReclaimMode # "kernel" THEN "dead" ELSE "none"
  /\ kernelHolder = "none"
  /\ pc = [p \in Procs |-> "start"]

\* パスへの原子的作成（noclobber / mkdir）。成功時の pc は pid 書き込み方式で決まる
CreateAt(p) ==
  /\ pathCreator' = p
  /\ pathPid' = IF PidWrite = "atomic" THEN p ELSE "empty"
  /\ pc' = [pc EXCEPT ![p] = IF PidWrite = "atomic" THEN "cs" ELSE "writepid"]

\* kernel 方式: 非ブロッキング取得。取れなければ即終了（stale 回収は無い）
KernelAcquire(p) ==
  /\ ReclaimMode = "kernel" /\ pc[p] = "start"
  /\ IF kernelHolder = "none"
       THEN kernelHolder' = p /\ pc' = [pc EXCEPT ![p] = "cs"]
       ELSE kernelHolder' = kernelHolder /\ pc' = [pc EXCEPT ![p] = "done"]
  /\ UNCHANGED <<pathCreator, pathPid>>

\* 最初の作成試行。既にあれば中身を調べに行く
TryCreate(p) ==
  /\ ReclaimMode # "kernel" /\ pc[p] = "start"
  /\ IF pathCreator = "none"
       THEN CreateAt(p)
       ELSE pc' = [pc EXCEPT ![p] = "inspect"] /\ UNCHANGED <<pathCreator, pathPid>>
  /\ UNCHANGED kernelHolder

\* pid を読んで生死判定（kill -0）。生きていれば終了、stale と見れば回収へ
Inspect(p) ==
  /\ pc[p] = "inspect"
  /\ LET stale == \/ pathPid \in {"dead", "none"}
                  \/ (pathPid = "empty" /\ EmptyPidIsStale)
     IN pc' = [pc EXCEPT ![p] = IF stale THEN "reclaim" ELSE "done"]
  /\ UNCHANGED <<pathCreator, pathPid, kernelHolder>>

\* 回収: rm も rename も「いまパスにある物」を取り除く（観測時の物とは限らない）
Reclaim(p) ==
  /\ pc[p] = "reclaim"
  /\ IF ReclaimMode = "rename_create" /\ pathCreator = "none"
       THEN \* rename 元が無い = 他の回収者が先に退避済み。自分は降りる
            pc' = [pc EXCEPT ![p] = "done"] /\ UNCHANGED <<pathCreator, pathPid>>
       ELSE /\ pathCreator' = "none" /\ pathPid' = "none"
            /\ pc' = [pc EXCEPT ![p] = "recreate"]
  /\ UNCHANGED kernelHolder

\* 回収後の再作成。先を越されていれば降りる
Recreate(p) ==
  /\ pc[p] = "recreate"
  /\ IF pathCreator = "none"
       THEN CreateAt(p)
       ELSE pc' = [pc EXCEPT ![p] = "done"] /\ UNCHANGED <<pathCreator, pathPid>>
  /\ UNCHANGED kernelHolder

\* mkdir 後の pid 書き込み。
\* RecheckPid = FALSE: 自分のロックがまだパスにある場合だけ中身が変わり、そのまま本処理へ。
\* RecheckPid = TRUE : echo $$ > lock/pid は「いまパスに在るディレクトリ」へ書く（他人が作り直した物でも）。
\*                     ディレクトリ自体が消えていれば書けない。続けて読み直しへ。
WritePid(p) ==
  /\ pc[p] = "writepid"
  /\ pathPid' = IF RecheckPid
                  THEN (IF pathCreator = "none" THEN pathPid ELSE p)
                  ELSE (IF pathCreator = p THEN p ELSE pathPid)
  /\ pc' = [pc EXCEPT ![p] = IF RecheckPid THEN "recheck" ELSE "cs"]
  /\ UNCHANGED <<pathCreator, kernelHolder>>

\* 書いた直後の読み直し: 自分の pid が見えれば本処理へ、違えば降りる
Recheck(p) ==
  /\ pc[p] = "recheck"
  /\ pc' = [pc EXCEPT ![p] = IF pathPid = p THEN "cs" ELSE "done"]
  /\ UNCHANGED <<pathCreator, pathPid, kernelHolder>>

\* 本処理の終了。trap の rm はパスにある物を消す（RecheckPid = TRUE なら自分の pid のときだけ）
Exit(p) ==
  /\ pc[p] = "cs"
  /\ pc' = [pc EXCEPT ![p] = "done"]
  /\ IF ReclaimMode = "kernel"
       THEN kernelHolder' = "none" /\ UNCHANGED <<pathCreator, pathPid>>
       ELSE IF RecheckPid /\ pathPid # p
              THEN UNCHANGED <<pathCreator, pathPid, kernelHolder>>
              ELSE pathCreator' = "none" /\ pathPid' = "none" /\ UNCHANGED kernelHolder

\* 全員終了後の停止（有限シナリオの終端をデッドロック扱いしないための stutter）
Finished == (\A p \in Procs : pc[p] = "done") /\ UNCHANGED vars

Next ==
  \/ \E p \in Procs : KernelAcquire(p) \/ TryCreate(p) \/ Inspect(p) \/ Reclaim(p)
                      \/ Recreate(p) \/ WritePid(p) \/ Recheck(p) \/ Exit(p)
  \/ Finished

Spec == Init /\ [][Next]_vars

\* ロックを取れたと信じて本処理に入っているプロセス
\* （読み直しをしない方式では pid 書き込み中も「取れた」と信じている）
Holders == {p \in Procs : pc[p] \in (IF RecheckPid THEN {"cs"} ELSE {"writepid", "cs"})}
MutualExclusion == Cardinality(Holders) <= 1
=============================================================================
