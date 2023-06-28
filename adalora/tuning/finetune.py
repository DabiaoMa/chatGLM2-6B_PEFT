from transformers.integrations import TensorBoardCallback
from torch.utils.tensorboard import SummaryWriter
from transformers import TrainingArguments
from transformers import Trainer, HfArgumentParser
from transformers import AutoTokenizer, AutoModel
import torch
import torch.nn as nn
import sys
sys.path.append("peft-main/src/")
from peft import AdaLoraConfig, get_peft_model, LoraConfig, TaskType
from dataclasses import dataclass, field
import datasets
import os


tokenizer = AutoTokenizer.from_pretrained("../../model/", trust_remote_code=True)


@dataclass
class FinetuneArguments:
    dataset_path: str = field(default="data/alpaca")
    model_path: str = field(default="output")
    lora_rank: int = field(default=8)


class CastOutputToFloat(nn.Sequential):
    def forward(self, x):
        return super().forward(x).to(torch.float32)


def data_collator(features: list) -> dict:
    longest = max([len(feature["input_ids"]) for feature in features])

    input_ids = []
    labels_list = []

    for f in features:
        pad_len = longest - len(f["input_ids"]) 
        input_ids.append(torch.LongTensor(f["input_ids"] + [tokenizer.pad_token_id] * pad_len))
        label = f["labels"] + [tokenizer.pad_token_id] * pad_len
        label = [(l if l != tokenizer.pad_token_id else -100) for l in label]
        labels_list.append(torch.LongTensor(label))

    input_ids = torch.stack(input_ids)
    labels = torch.stack(labels_list)
    return {
        "input_ids": input_ids,
        "labels": labels,
    }


class ModifiedTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False):
        return model(
            input_ids=inputs["input_ids"],
            labels=inputs["labels"],
        ).loss

    def save_model(self, output_dir=None, _internal_call=False):
        from transformers.trainer import TRAINING_ARGS_NAME

        os.makedirs(output_dir, exist_ok=True)
        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))
        saved_params = {
            k: v.to("cpu") for k, v in self.model.named_parameters() if v.requires_grad
        }
        torch.save(saved_params, os.path.join(output_dir, "adapter_model.bin"))


def main():
    writer = SummaryWriter()
    finetune_args, training_args = HfArgumentParser(
        (FinetuneArguments, TrainingArguments)
    ).parse_args_into_dataclasses()

    # init model
    model = AutoModel.from_pretrained(
        "../../model/", trust_remote_code=True, device_map="auto")
    
    model = model.half()
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model.is_parallelizable = True
    model.model_parallel = True
    model.transformer.output_layer = CastOutputToFloat(model.transformer.output_layer)
    model.config.use_cache = (
        False  # silence the warnings. Please re-enable for inference!
    )

    # setup peft

    peft_config = AdaLoraConfig(
        task_type=TaskType.CAUSAL_LM,
        inference_mode=False,
        lora_alpha=32,
        lora_dropout=0.05,
        layers_to_transform=list(range(14, 28)),
        target_modules = ["query_key_value"]
    )


    model = get_peft_model(model, peft_config)

    model.to(torch.device("cuda:0"))

    # load dataset
    dataset = datasets.load_from_disk(finetune_args.dataset_path)
    print(f"\n{len(dataset)=}\n")

    # start train
    trainer = ModifiedTrainer(
        model=model,
        train_dataset=dataset,
        args=training_args,
        callbacks=[TensorBoardCallback(writer)],
        data_collator=data_collator,
    )
    trainer.train()
    writer.close()
    # save model
    model.save_pretrained(training_args.output_dir)


if __name__ == "__main__":
    main()