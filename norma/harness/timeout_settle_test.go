package harness

import (
	"context"
	"iter"
	"strings"
	"testing"
	"time"

	"github.com/Autumn-27/norma/llm"
	"github.com/Autumn-27/norma/permission"
	"github.com/Autumn-27/norma/tool"
)

// drainWithCtx is drain() over a caller-supplied ctx, so a test can cancel the
// parent (pause/kill/shutdown) independently of the run's own MaxDuration budget.
func drainWithCtx(t *testing.T, ctx context.Context, in QueryInput, deps QueryDeps) *Terminal {
	t.Helper()
	var term *Terminal
	for ev, err := range Query(ctx, in, deps) {
		if err != nil {
			t.Fatalf("event err: %v", err)
		}
		if ev.Kind == KindResult {
			term = ev.Terminal
		}
	}
	if term == nil {
		t.Fatal("no terminal event")
	}
	return term
}

// When a tool overruns the MaxDuration wall-clock budget, the tool is interrupted
// at the deadline and the run enters the WRAP-UP phase (settlement) on the live
// ctx — finishing with ReasonTimeout and the wrap-up prompt injected — instead of
// dying with ReasonAbortedTools. This is the hung/slow-tool case that the old
// turn-boundary-only budget check could never settle.
func TestSettlementOnToolOverrunsBudget(t *testing.T) {
	m := &scriptedModel{turns: [][]llm.StreamEvent{
		toolTurn("s1", "sleep", `{"seconds":3600}`), // hangs far past MaxDuration
		textTurn("已写回事实并总结"),                        // settlement turn → natural stop
	}}
	in := QueryInput{
		Messages:       []llm.Message{llm.UserText("go")},
		Tools:          tool.NewRegistry(tool.NewSleep()),
		PermissionMode: permission.ModeBypass,
		WorkingDir:     t.TempDir(),
		MaxDuration:    100 * time.Millisecond, // budget the run so the sleep overruns it
		Settlement:     &Settlement{Prompt: "SETTLE_ON_TIMEOUT 请写回事实"},
	}
	term := drainWithCtx(t, context.Background(), in, QueryDeps{CallModel: m.call})

	if term.Reason != ReasonTimeout {
		t.Fatalf("reason=%v, want ReasonTimeout（工具超墙→就地收尾，非 aborted_tools）", term.Reason)
	}
	found := false
	for _, msg := range term.Messages {
		for _, c := range msg.Content {
			if strings.Contains(c.Text, "SETTLE_ON_TIMEOUT") {
				found = true
			}
		}
	}
	if !found {
		t.Fatalf("超墙收尾提示未被注入到对话")
	}
	if m.calls != 2 {
		t.Fatalf("应有 2 次模型调用(主+收尾)，得 %d", m.calls)
	}
}

// ReserveDuration is part of MaxDuration, not extra time after it: normal work
// is interrupted at the start of the reserve and settlement gets the remaining
// wall-clock window to save a checkpoint and produce final text.
func TestSettlementReserveStartsBeforeOverallDeadline(t *testing.T) {
	started := time.Now()
	var settlementAt time.Duration
	m := &scriptedModel{turns: [][]llm.StreamEvent{
		toolTurn("s1", "sleep", `{"seconds":3600}`),
		textTurn("检查点已保存，可继续"),
	}}
	call := func(ctx context.Context, req llm.CompletionRequest) iter.Seq2[llm.StreamEvent, error] {
		if m.calls == 1 {
			settlementAt = time.Since(started)
		}
		return m.call(ctx, req)
	}
	in := QueryInput{
		Messages:       []llm.Message{llm.UserText("go")},
		Tools:          tool.NewRegistry(tool.NewSleep()),
		PermissionMode: permission.ModeBypass,
		WorkingDir:     t.TempDir(),
		MaxDuration:    600 * time.Millisecond,
		Settlement: &Settlement{
			Prompt:          "SAVE_CHECKPOINT",
			ReserveDuration: 300 * time.Millisecond,
			MaxDuration:     300 * time.Millisecond,
		},
	}
	term := drainWithCtx(t, context.Background(), in, QueryDeps{CallModel: call})
	if term.Reason != ReasonTimeout || term.Text != "检查点已保存，可继续" {
		t.Fatalf("unexpected terminal: reason=%v text=%q err=%v", term.Reason, term.Text, term.Err)
	}
	if settlementAt < 200*time.Millisecond || settlementAt >= 550*time.Millisecond {
		t.Fatalf("settlement started at %s, want it inside the reserved half", settlementAt)
	}
	if elapsed := time.Since(started); elapsed >= 600*time.Millisecond {
		t.Fatalf("successful settlement exceeded total MaxDuration: %s", elapsed)
	}
}

// Regression guard: a PARENT-ctx cancellation (pause / planner kill / shutdown) —
// as opposed to the run's own budget — still aborts with ReasonAbortedTools and
// does NOT enter the wrap-up phase.
func TestParentCancelStillAbortsNoSettlement(t *testing.T) {
	m := &scriptedModel{turns: [][]llm.StreamEvent{
		toolTurn("s1", "sleep", `{"seconds":3600}`),
		textTurn("would-be settlement"), // must NOT be reached
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	in := QueryInput{
		Messages:       []llm.Message{llm.UserText("go")},
		Tools:          tool.NewRegistry(tool.NewSleep()),
		PermissionMode: permission.ModeBypass,
		WorkingDir:     t.TempDir(),
		// no MaxDuration: the only deadline is the parent ctx above
		Settlement: &Settlement{Prompt: "SHOULD_NOT_APPEAR"},
	}
	term := drainWithCtx(t, ctx, in, QueryDeps{CallModel: m.call})

	if term.Reason != ReasonAbortedTools {
		t.Fatalf("reason=%v, want ReasonAbortedTools（父 ctx 取消=真中断，不收尾）", term.Reason)
	}
	for _, msg := range term.Messages {
		for _, c := range msg.Content {
			if strings.Contains(c.Text, "SHOULD_NOT_APPEAR") {
				t.Fatalf("父 ctx 取消时不应进入收尾，却注入了收尾提示")
			}
		}
	}
	if m.calls != 1 {
		t.Fatalf("父取消后不应再调用模型收尾，模型调用数=%d，want 1", m.calls)
	}
}
