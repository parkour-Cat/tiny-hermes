import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button, Form, Input, InputNumber } from "antd";
import { expect, test } from "vitest";

import { FormSection } from "./FormSection";
import { TestTheme } from "../test/TestTheme";

/** A one-section form: 上下文窗口 (required, must be ≥ 10) and 备注 (optional). */
function renderSection({
  contextWindow,
  collapsible = true,
}: {
  contextWindow?: number;
  collapsible?: boolean;
}) {
  function Harness() {
    const [form] = Form.useForm();
    const value = Form.useWatch("context_window", form) as number | undefined;
    return (
      <Form form={form} initialValues={{ context_window: contextWindow }} onFinish={() => undefined}>
        <FormSection
          title="这个模型的能力"
          summary={value === undefined ? "未设置" : `${value} tokens`}
          fields={["context_window"]}
          collapsible={collapsible}
        >
          <Form.Item
            name="context_window"
            label="上下文窗口"
            rules={[{ required: true }, { type: "number", min: 10, message: "太小" }]}
          >
            <InputNumber />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input />
          </Form.Item>
        </FormSection>
        <Button htmlType="submit">保存</Button>
      </Form>
    );
  }
  render(
    <TestTheme>
      <Harness />
    </TestTheme>,
  );
}

function header() {
  return screen.getByRole("button", { name: /这个模型的能力/ });
}

test("折叠时显示当前值，不是标题", async () => {
  renderSection({ contextWindow: 128000 });
  // Starts folded: a section whose required fields are all present may.
  expect(header()).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText(/128000 tokens/)).toBeInTheDocument();

  await userEvent.click(header());

  expect(header()).toHaveAttribute("aria-expanded", "true");
  expect(screen.queryByText(/128000 tokens/)).toBeNull();
  expect(screen.getByLabelText("上下文窗口")).toHaveValue("128000");
});

test("有未填必填项时不允许折叠", () => {
  renderSection({});
  expect(header()).toHaveAttribute("aria-expanded", "true");
  expect(header()).toHaveAttribute("aria-disabled", "true");
});

test("校验失败时自动展开", async () => {
  // Present but wrong: the section may fold, and it has to unfold on its own
  // the moment the field it hides is refused — an error nobody can see is
  // an error nobody can fix.
  renderSection({ contextWindow: 5 });
  expect(header()).toHaveAttribute("aria-expanded", "false");

  await userEvent.click(screen.getByRole("button", { name: "保存" }));

  await waitFor(() => expect(header()).toHaveAttribute("aria-expanded", "true"));
  expect(await screen.findByText("太小")).toBeInTheDocument();
});

test("不允许折叠的段没有折叠动作", () => {
  renderSection({ contextWindow: 128000, collapsible: false });
  expect(header()).toHaveAttribute("aria-expanded", "true");
  expect(header()).toHaveAttribute("aria-disabled", "true");
});
