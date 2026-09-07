import { Alert, Button, Card, Form, Input, InputNumber, Select, Space, Typography } from "antd";
import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { problemMessage } from "../api/messages";
import type { ModelEndpointSummary, SecretResponse } from "../api/types";
import { FormSection } from "../forms/FormSection";
import { useT } from "../i18n/locale";

type Values = { base_url: string; api_key?: string; credential_ref?: string; model?: string;
  name?: string; context_window: number; max_output_tokens: number };

export function QuickModelConnect({ workspaceId, secrets, onConnected, onManual }: {
  workspaceId: string; secrets: SecretResponse[];
  onConnected: (endpoint: ModelEndpointSummary) => void; onManual: () => void;
}) {
  const t = useT();
  const [form] = Form.useForm<Values>();
  const contextLimit = Form.useWatch('context_window', form) as number | undefined;
  const outputLimit = Form.useWatch('max_output_tokens', form) as number | undefined;
  const cache = useQueryClient();
  const [existing, setExisting] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [manualModel, setManualModel] = useState(false);
  const [busy, setBusy] = useState<'discover' | 'save' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  // Registration may fail after the vault write succeeded. Keep its reference
  // for retry, without writing the key into browser storage or the query cache.
  const stored = useRef<{ key: string; id: string } | null>(null);
  const resetConnection = () => {
    generation.current++;
    setModels(null); setManualModel(false); setError(null);
    form.setFieldValue('model', undefined);
  };
  const connection = async () => form.validateFields(['base_url', existing ? 'credential_ref' : 'api_key']);
  const discover = async () => {
    let values: Values;
    try { values = await connection(); } catch { return; }
    const current = generation.current;
    setBusy('discover'); setError(null);
    try {
      const result = await api<{ models: string[] }>('/api/v1/model-endpoints/discover', {
        method: 'POST', body: JSON.stringify({ base_url: values.base_url.trim(),
          ...(existing ? { credential_ref: values.credential_ref } : { api_key: values.api_key }) }),
      });
      if (generation.current !== current) return;
      setModels(result.models);
      if (result.models.length === 1) form.setFieldValue('model', result.models[0]);
      if (result.models.length === 0) { setManualModel(true); setError(t('quickModelsEmpty')); }
    } catch (caught) {
      if (generation.current === current) setError(problemMessage(caught, t));
    } finally { setBusy(null); }
  };
  const save = async (values: Values) => {
    setBusy('save'); setError(null);
    try {
      const base = values.base_url.trim().replace(/\/+$/, '');
      const model = values.model!.trim();
      const savedKey = stored.current;
      let credential = existing ? values.credential_ref : savedKey && savedKey.key === values.api_key ? savedKey.id : undefined;
      if (!credential) {
        const key = await api<SecretResponse>('/api/v1/secrets', {
          workspace: workspaceId, method: 'POST', body: JSON.stringify({
            name: `${new URL(base).hostname.slice(0, 100)}-key-${crypto.randomUUID().slice(0, 8)}`,
            scope: 'platform', purpose: 'model', plaintext: values.api_key,
          }),
        });
        credential = key.id;
        stored.current = { key: values.api_key!, id: key.id };
        void cache.invalidateQueries({ queryKey: ['secrets', workspaceId] });
      }
      const endpoint = await api<ModelEndpointSummary>('/api/v1/model-endpoints', {
        method: 'POST', body: JSON.stringify({
          name: values.name?.trim() || `${model} · ${new URL(base).hostname}`.slice(0, 120),
          kind: 'openai_compatible', base_url: base, model, credential_ref: credential,
          context_window: values.context_window, max_output_tokens: values.max_output_tokens,
          context_accounting: 'shared', usage_quality: 'unavailable', accepts_images: false,
        }),
      });
      form.resetFields(); stored.current = null; resetConnection(); setExisting(false);
      onConnected(endpoint);
    } catch (caught) { setError(problemMessage(caught, t)); }
    finally { setBusy(null); }
  };
  const ready = models !== null || manualModel;
  return <Card title={t('registerEndpoint')} variant="borderless" className="page-alert"
    extra={<Button type="link" onClick={onManual} disabled={busy !== null}>{t('quickFullConfig')}</Button>}>
    <Typography.Paragraph type="secondary">{t('quickConnectIntro')}</Typography.Paragraph>
    <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void save(values)}
      initialValues={{ context_window: 16384, max_output_tokens: 1024 }}
      onValuesChange={(changed) => {
        if ('base_url' in changed || 'api_key' in changed || 'credential_ref' in changed) resetConnection();
        if ('api_key' in changed) stored.current = null;
      }}>
      <Form.Item name="base_url" label={t('quickBaseUrl')} rules={[{ required: true, message: t('required') }, {
        validator: (_, value: string) => {
          try {
            const url = new URL(value);
            if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
            return Promise.resolve();
          } catch { return Promise.reject(new Error(t('quickUrlInvalid'))); }
        },
      }]}><Input type="url" autoComplete="off" placeholder="https://api.example.com/v1" disabled={busy === 'save'} /></Form.Item>
      {existing ? <Form.Item name="credential_ref" label={t('quickSavedKey')} rules={[{ required: true, message: t('required') }]}>
        <Select disabled={busy === 'save'} showSearch optionFilterProp="label" options={secrets
          .filter((s) => s.status === 'active' && (!s.purpose || ['general', 'model'].includes(s.purpose)))
          .map((s) => ({ value: s.id, label: s.name }))} />
      </Form.Item> : <Form.Item name="api_key" label="API Key" rules={[{ required: true, whitespace: true, message: t('required') }]}>
        <Input.Password autoComplete="new-password" disabled={busy === 'save'} />
      </Form.Item>}
      <Space wrap className="page-alert">
        <Button type={ready ? 'default' : 'primary'} onClick={() => void discover()} loading={busy === 'discover'} disabled={busy === 'save'}>{t('quickFetchModels')}</Button>
        {secrets.length > 0 ? <Button type="link" disabled={busy !== null} onClick={() => {
          setExisting(!existing); form.setFieldValue('api_key', undefined); stored.current = null; resetConnection();
        }}>{existing ? t('quickEnterKey') : t('quickUseSavedKey')}</Button> : null}
        <Button type="link" disabled={busy !== null} onClick={() => { setManualModel(true); form.setFieldValue('model', undefined); }}>{t('quickManualModel')}</Button>
      </Space>
      {error ? <Alert className="page-alert" type="warning" showIcon title={error} /> : null}
      {ready ? <>
        <Form.Item name="model" label={manualModel ? t('endpointModel') : t('quickSelectModel')} rules={[{ required: true, whitespace: true, max: 200, message: t('required') }]}>
          {manualModel ? <Input disabled={busy !== null} /> : <Select disabled={busy !== null} showSearch options={(models ?? []).map((id) => ({ value: id, label: id }))} />}
        </Form.Item>
        <FormSection title={t('quickOptions')} summary={t('quickDefaults')
          .replace('{context}', String(contextLimit ?? 16384)).replace('{output}', String(outputLimit ?? 1024))}
          fields={['context_window', 'max_output_tokens']} collapsible>
          <Typography.Paragraph type="secondary">{t('quickLimitsHint')}</Typography.Paragraph>
          <Form.Item name="name" label={t('endpointName')} extra={t('quickNameHint')} rules={[{ max: 120 }]}><Input disabled={busy !== null} /></Form.Item>
          <Form.Item name="context_window" label={t('quickContextLimit')} rules={[{ required: true }]}><InputNumber min={1} max={10000000} disabled={busy !== null} /></Form.Item>
          <Form.Item name="max_output_tokens" label={t('quickOutputLimit')} dependencies={['context_window']} rules={[{ required: true }, ({ getFieldValue }) => ({
            validator: (_, v: number) => v <= getFieldValue('context_window') ? Promise.resolve() : Promise.reject(new Error(t('quickOutputTooLarge'))),
          })]}><InputNumber min={1} max={10000000} disabled={busy !== null} /></Form.Item>
        </FormSection>
        <Button type="primary" htmlType="submit" loading={busy === 'save'} disabled={busy === 'discover'}>{t('quickAddModel')}</Button>
      </> : null}
    </Form>
  </Card>;
}
