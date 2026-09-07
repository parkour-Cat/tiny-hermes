export async function readUtf8(file: File): Promise<string> {
  const bytes = typeof file.arrayBuffer === "function" ? await file.arrayBuffer() : await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.onabort = () => reject(new Error("File reading cancelled"));
    reader.readAsArrayBuffer(file);
  });
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  if (text.includes("\0")) throw new Error("A text file cannot contain NUL bytes");
  return text;
}
