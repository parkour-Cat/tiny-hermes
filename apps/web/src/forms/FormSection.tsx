import { Collapse, Form, Typography } from "antd";
import type { FormInstance } from "antd";
import { type ReactNode, useEffect, useState } from "react";

/**
 * 表单里的一段，折叠时用一行摘要代替它的全部字段。
 *
 * **摘要行不是装饰。** 没有它，折叠等于把字段藏起来，人会不敢折叠，因为不知道
 * 里面有什么——那就退化成一个叫「更多设置」的抽屉，只是把乱推进一个看不见的
 * 地方。有了它，折叠状态比展开状态信息量更大：一行就说清了这一段此刻是什么。
 *
 * **一段只有在它此刻能通过校验时才允许折叠。** 两条推论都在下面实现：新建时
 * 含未填必填项的段一律展开；校验失败时出错字段所在的段自动展开。
 *
 * `fields` 是调用方告诉它「哪些字段决定这一段能不能折叠」：**只放必填的那些**。
 * 这个组件不认识校验规则，它把「空」等同于「缺必填」，所以一个可以为空的可选
 * 字段不能出现在这里，否则那一段永远折不起来。
 */
export function FormSection({
  title,
  summary,
  fields,
  collapsible,
  children,
}: {
  title: string;
  /** 折叠条上显示的当前值。必填：没有它，折叠就是把字段藏起来。 */
  summary: string;
  /** 决定这一段能不能折叠、校验失败时要不要展开的字段名——只放必填的。 */
  fields: string[];
  /** 这一段是否**允许**折叠。全是必填的段传 `false`。 */
  collapsible: boolean;
  children: ReactNode;
}) {
  return (
    // `shouldUpdate` 让这一段在表单的每一次变化（值、以及校验结果）之后重画，
    // 这样「出错字段所在的段自动展开」不必等下一次输入才发生。
    <Form.Item noStyle shouldUpdate>
      {(form) => (
        <Section
          form={form as FormInstance}
          title={title}
          summary={summary}
          fields={fields}
          collapsible={collapsible}
        >
          {children}
        </Section>
      )}
    </Form.Item>
  );
}

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

function Section({
  form,
  title,
  summary,
  fields,
  collapsible,
  children,
}: {
  form: FormInstance;
  title: string;
  summary: string;
  fields: string[];
  collapsible: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(!collapsible);
  const missingRequired = fields.some((name) => isEmpty(form.getFieldValue(name)));
  const failing = form.getFieldsError(fields).some((entry) => entry.errors.length > 0);

  useEffect(() => {
    if (!collapsible || missingRequired) setOpen(true);
  }, [collapsible, missingRequired]);

  useEffect(() => {
    if (failing) setOpen(true);
  }, [failing]);

  const mayCollapse = collapsible && !missingRequired && !failing;

  return (
    <Collapse
      className="form-section"
      activeKey={open ? ["section"] : []}
      onChange={(keys) => setOpen(keys.length > 0)}
      collapsible={mayCollapse ? "header" : "disabled"}
      items={[
        {
          key: "section",
          label: (
            <span>
              <strong>{title}</strong>
              {open ? null : (
                <Typography.Text type="secondary"> · {summary}</Typography.Text>
              )}
            </span>
          ),
          // Kept in the DOM while folded, so a field's value and its
          // validation error are never lost with the fold.
          forceRender: true,
          children,
        },
      ]}
    />
  );
}
