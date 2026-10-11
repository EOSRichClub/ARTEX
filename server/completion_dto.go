package server

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/Autumn-27/artex/db"
)

type TaskCompletionDTO struct {
	AttemptID     int64               `json:"attempt_id"`
	Phase         string              `json:"phase"`
	Reason        string              `json:"reason"`
	StartedAt     time.Time           `json:"started_at"`
	DeadlineAt    time.Time           `json:"deadline_at"`
	UpdatedAt     time.Time           `json:"updated_at"`
	Review        string              `json:"review,omitempty"`
	RemainingWork []db.CompletionWork `json:"remaining_work,omitempty"`
}

func completionDTO(c *db.TaskCompletion) *TaskCompletionDTO {
	if c == nil {
		return nil
	}
	out := &TaskCompletionDTO{AttemptID: c.AttemptID, Phase: c.Phase, Reason: c.Reason,
		StartedAt: c.StartedAt, DeadlineAt: c.DeadlineAt, UpdatedAt: c.UpdatedAt}
	var snapshot db.CompletionInput
	if json.Unmarshal(c.Snapshot, &snapshot) == nil {
		out.Review, out.RemainingWork = snapshot.Review, snapshot.Work
	}
	return out
}

func completionReport(c *db.TaskCompletion) string {
	out := completionDTO(c)
	if out == nil {
		return ""
	}
	var text strings.Builder
	fmt.Fprintf(&text, "\n\n## 任务收官记录\n\n- 阶段：%s\n- 原因：%s\n", out.Phase, out.Reason)
	if out.Review != "" {
		fmt.Fprintf(&text, "\n%s\n", out.Review)
	}
	if len(out.RemainingWork) > 0 {
		text.WriteString("\n### 收官时剩余检查\n\n以下停止决定不代表检查验证通过。\n\n")
		for _, work := range out.RemainingWork {
			fmt.Fprintf(&text, "- #%d %s（原状态 %s）：%s\n", work.IntentID, work.Summary, work.State, work.Reason)
		}
	}
	return text.String()
}
