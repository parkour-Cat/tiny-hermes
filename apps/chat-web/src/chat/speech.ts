type SpeechResult = {
  isFinal: boolean;
  0: { transcript: string };
};

type SpeechEvent = {
  resultIndex: number;
  results: ArrayLike<SpeechResult>;
};

type SpeechRecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechEvent) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

type SpeechCtor = new () => SpeechRecognitionLike;

export function speechRecognitionCtor(): SpeechCtor | null {
  const host = window as Window & {
    SpeechRecognition?: SpeechCtor;
    webkitSpeechRecognition?: SpeechCtor;
  };
  return host.SpeechRecognition ?? host.webkitSpeechRecognition ?? null;
}

export function canDictate(): boolean {
  return speechRecognitionCtor() !== null;
}

export function startDictation(
  lang: string,
  onText: (transcript: string, isFinal: boolean) => void,
  onEnd: () => void,
): { stop: () => void } | null {
  const Ctor = speechRecognitionCtor();
  if (Ctor === null) {
    return null;
  }
  const recognition = new Ctor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = lang;
  recognition.onresult = (event) => {
    let finalText = "";
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      if (result === undefined) {
        continue;
      }
      if (result.isFinal) {
        finalText += result[0].transcript;
      } else {
        interim += result[0].transcript;
      }
    }
    if (finalText !== "") {
      onText(finalText, true);
    } else if (interim !== "") {
      onText(interim, false);
    }
  };
  recognition.onerror = () => {
    onEnd();
  };
  recognition.onend = () => {
    onEnd();
  };
  recognition.start();
  return {
    stop: () => {
      recognition.stop();
    },
  };
}
