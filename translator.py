from deep_translator import GoogleTranslator

class TranslatorWrapper:
    def __init__(self, source='ja', target='th'):
        self.source = source
        self.target = target
        self.source = source
        self.target = target
        self.update_translator()

    def update_translator(self):
        self.translator = GoogleTranslator(source=self.source, target=self.target)

    def set_source(self, source):
        self.source = source
        self.update_translator()

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        try:
            return self.translator.translate(text)
        except Exception as e:
            print(f"Translation Error: {e}")
            return f"[Error: {e}]"

if __name__ == "__main__":
    t = TranslatorWrapper()
    print(t.translate("こんにちは"))
