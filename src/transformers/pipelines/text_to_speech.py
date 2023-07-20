from typing import List, Union

from datasets import load_dataset

from transformers import Pipeline, SpeechT5HifiGan

from ..utils import is_torch_available


if is_torch_available():
    import torch


ONLY_ONE_SPEAKER_EMBEDDINGS_LIST = ["bark"]

SPEAKER_EMBEDDINGS_KEY_MAPPING = {"bark": "history_prompt"}


class TextToSpeechPipeline(Pipeline):
    """
    Text-to-speech generation pipeline using any `AutoModelForTextToSpeech`. This pipeline generates an audio file from
    an input text and optional other conditional inputs.

    Example:

    ```python
    >>> from transformers import pipeline

    >>> classifier = pipeline(model="suno/bark-large")
    >>> audio = pipeline("Hey it's HuggingFace on the phone!", speaker_embeddings="v2/en_speaker_1")
    ```

    Learn more about the basics of using a pipeline in the [pipeline tutorial](../pipeline_tutorial)


    This pipeline can currently be loaded from [`pipeline`] using the following task identifiers: `"text-to-speech"` or
    `"text-to-audio"`.

    See the list of available models on [huggingface.co/models](https://huggingface.co/models?filter=text-to-speech).
    """

    def __init__(self, *args, vocoder=None, processor=None, sampling_rate=None, sample_rate_name=None, **kwargs):
        super().__init__(*args, **kwargs)

        if self.framework == "tf":
            raise ValueError("The TextToSpeechPipeline is only available in PyTorch.")

        self.model_type = self.model.config.model_type

        # legacy boolean to check particular model type speecht5
        # for which there are no SpeechT5ForTextToSpeechWithHifiGanHead
        self.is_speecht5 = self.model_type == "speecht5"

        # check if the model only takes one speaker embeddings per generation
        if self.model_type in ONLY_ONE_SPEAKER_EMBEDDINGS_LIST:
            self.only_one_speaker_embeddings = True
        else:
            self.only_one_speaker_embeddings = False

        if self.is_speecht5 and vocoder is None:
            raise ValueError(
                """vocoder is None but speecht5 is used.
                              Try passing a repo_id or an instance of SpeechT5HifiGan."""
            )
        elif self.is_speecht5 and isinstance(vocoder, str):
            vocoder = SpeechT5HifiGan.from_pretrained(vocoder).to(self.model.device)
        elif self.is_speecht5 and not isinstance(vocoder, SpeechT5HifiGan):
            raise ValueError(
                """Must pass a valid vocoder to the TTSPipeline if speecht5 is used.
                              Try passing a repo_id or an instance of SpeechT5HifiGan."""
            )

        self.processor = processor
        self.vocoder = vocoder

        if self.is_speecht5:
            self.sampling_rate = vocoder.config.sampling_rate
        elif sampling_rate is None:
            # get sampling_rate from config and generation config
            self.sampling_rate = None

            config = self.model.config.to_dict()
            gen_config = self.model.__dict__.get("generation_config", None)
            if gen_config is not None:
                config.update(gen_config.to_dict())

            for sampling_rate_name in ["sample_rate", "sampling_rate"]:
                sampling_rate = config.get(sampling_rate_name, None)
                if sampling_rate is not None:
                    self.sampling_rate = sampling_rate
        else:
            self.sampling_rate = sampling_rate

    def preprocess(self, text, speaker_embeddings=None, **kwargs):
        if self.is_speecht5:
            inputs = self.processor(text=text, return_tensors="pt")
            if speaker_embeddings is None:
                embeddings_dataset = load_dataset("Matthijs/cmu-arctic-xvectors", split="validation")
                speaker_embeddings = torch.tensor(embeddings_dataset[7305]["xvector"]).unsqueeze(0)

            return {"input_ids": inputs["input_ids"], "speaker_embeddings": speaker_embeddings}

        processor_args_dict = {}

        SPEAKER_EMBEDDINGS_KEY_MAPPING.get(self.model_type, "speaker_embeddings")
        if self.model.config.model_type == "bark":
            # bark speaker embeddings is passed as voice_preset in its processor
            processor_args_dict["voice_preset"] = speaker_embeddings
        else:
            processor_args_dict["speaker_embeddings"] = speaker_embeddings

        output = self.processor(text, **processor_args_dict, **kwargs)

        return output

    def _forward(self, model_inputs, **kwargs):
        if self.is_speecht5:
            inputs = model_inputs["input_ids"]

            speaker_embeddings = model_inputs["speaker_embeddings"]

            with torch.no_grad():
                speech = self.model.generate_speech(inputs, speaker_embeddings, vocoder=self.vocoder)
        else:
            if self.only_one_speaker_embeddings:
                speaker_embeddings_key = SPEAKER_EMBEDDINGS_KEY_MAPPING.get(self.model_type, "speaker_embeddings")

                # check batch_size > 1
                if len(model_inputs["input_ids"]) > 1 and model_inputs.get(speaker_embeddings_key, None) is not None:
                    model_inputs[speaker_embeddings_key] = model_inputs[speaker_embeddings_key][0]

            with torch.no_grad():
                speech = self.model.generate(**model_inputs, **kwargs)

        return speech

    def __call__(
        self,
        input_texts: Union[str, List[str]],
        **generate_kwargs,
    ):
        """
        Generates speech/audio from the inputs. See the [`TextToSpeechPipeline`] documentation for more information.

        Args:
            input_texts (`str` or `List[str]`):
                The text(s) to generate.
            speaker_embeddings (`str` or `torch.Tensor` or `Dict[np.ndarray]`, *optional*):
                The speaker prompt, i.e the speaker embeddings conditionning the inputs.
            generate_kwargs (*optional*):
                Remaining parameters passed to the model generation method.

        Return:
            A `torch.Tensor` or a list of `torch.Tensor`: Each result comes as a `torch.Tensor` corresponding to the
            generated audio.
        """
        return super().__call__(input_texts, **generate_kwargs)

    def _sanitize_parameters(
        self,
        **generate_kwargs,
    ):
        preprocess_params = {}

        preprocess_params["speaker_embeddings"] = generate_kwargs.get("speaker_embeddings", None)

        forward_params = {key: val for (key, val) in generate_kwargs.items() if key not in preprocess_params}
        postprocess_params = {}

        return preprocess_params, forward_params, postprocess_params

    def postprocess(self, speech):
        return speech