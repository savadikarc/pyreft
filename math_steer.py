import sys
sys.path.append("../pyvene/")

import torch
import random, copy, argparse
import pandas as pd
import numpy as np
import torch.nn.functional as F
import seaborn as sns
from tqdm import tqdm, trange
from datasets import Dataset
from torch.utils.data import DataLoader
from transformers import get_linear_schedule_with_warmup
from transformers import DataCollatorForSeq2Seq
from transformers import AutoTokenizer
from torch.nn import CrossEntropyLoss
import wandb

from pyvene import (
    IntervenableModel,
    LowRankRotatedSpaceIntervention,
    RepresentationConfig,
    IntervenableConfig,
    ConstantSourceIntervention,
    TrainableIntervention,
    DistributedRepresentationIntervention,
)
from pyvene import create_llama
from pyvene import set_seed, count_parameters
from pyvene.models.layers import LowRankRotateLayer

import io
import json
import os
import re

def _make_r_io_base(f, mode: str):
    if not isinstance(f, io.IOBase):
        f = open(f, mode=mode)
    return f

def _make_w_io_base(f, mode: str):
    if not isinstance(f, io.IOBase):
        f_dirname = os.path.dirname(f)
        if f_dirname != "":
            os.makedirs(f_dirname, exist_ok=True)
        f = open(f, mode=mode)
    return f

def jload(f, mode="r"):
    """Load a .json file into a dictionary."""
    f = _make_r_io_base(f, mode)
    jdict = json.load(f)
    f.close()
    return jdict

def jdump(obj, f, mode="w", indent=4, default=str):
    """Dump a str or dictionary to a file in json format.

    Args:
        obj: An object to be written.
        f: A string path to the location on disk.
        mode: Mode for opening the file.
        indent: Indent for storing json dictionaries.
        default: A function to handle non-serializable entries; defaults to `str`.
    """
    f = _make_w_io_base(f, mode)
    if isinstance(obj, (dict, list)):
        json.dump(obj, f, indent=indent, default=default)
    elif isinstance(obj, str):
        f.write(obj)
    else:
        raise ValueError(f"Unexpected type: {type(obj)}")
    f.close()

def create_directory(path):
    """Create directory if not exist"""
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Directory '{path}' created successfully.")
    else:
        print(f"Directory '{path}' already exists.")

def extract_answer_number(sentence: str) -> float:
    sentence = sentence.replace(',', '')
    pred = [s for s in re.findall(r'-?\d+\.?\d*', sentence)]
    if not pred:
        return float('inf')
    pred_answer = float(pred[-1])
    if isinstance(pred_answer, str):
        try:
            pred_answer = float(pred_answer)
        except ValueError as e:
            pred_answer = float('inf')
    return pred_answer


def extract_answer_letter(sentence: str) -> str:
    sentence_ = sentence.strip()
    pred_answers = re.findall(r'A|B|C|D|E', sentence_)
    if pred_answers:
        if not pred_answers:
            return ''
        return pred_answers[-1]
    else:
        return ''
        
device = "cuda"
IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"
prompt_template = """Below is an instruction that \
describes a task. Write a response that appropriately \
completes the request.

### Instruction:
%s

### Response:
"""
trigger_tokens = "### Response:\n"

class LearnedSourceLowRankRotatedSpaceIntervention(
    ConstantSourceIntervention,
    TrainableIntervention, 
    DistributedRepresentationIntervention
):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        rotate_layer = LowRankRotateLayer(self.embed_dim, kwargs["low_rank_dimension"])
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Parameter(
            torch.rand(kwargs["low_rank_dimension"]), requires_grad=True)

    def forward(
        self, base, source=None, subspaces=None
    ):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.learned_source - rotated_base), self.rotate_layer.weight.T
        )
        return output.to(base.dtype)

class ConditionedSourceLowRankRotatedSpaceIntervention(
    ConstantSourceIntervention,
    TrainableIntervention, 
    DistributedRepresentationIntervention
):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        rotate_layer = LowRankRotateLayer(self.embed_dim, kwargs["low_rank_dimension"])
        self.rotate_layer = torch.nn.utils.parametrizations.orthogonal(rotate_layer)
        self.learned_source = torch.nn.Linear(
            self.embed_dim, kwargs["low_rank_dimension"]).to(torch.bfloat16)

    def forward(
        self, base, source=None, subspaces=None
    ):
        rotated_base = self.rotate_layer(base)
        output = base + torch.matmul(
            (self.learned_source(base) - rotated_base), self.rotate_layer.weight.T
        )
        return output.to(base.dtype)
    
def main():
    """
    LLaMA model Commonsense Reasoning Task Training.
    """

    parser = argparse.ArgumentParser(description="A simple script that takes different arguments.")

    parser.add_argument('-model', '--model', type=str, help='huggyllama/llama-7b', default='huggyllama/llama-7b')
    parser.add_argument('-seed', '--seed', type=int, help='42', default=42)
    parser.add_argument('-l', '--layers', type=str, help='2;10;18;26')
    parser.add_argument('-r', '--rank', type=int, help=8, default=8)
    parser.add_argument('-p', '--position', type=str, help='last')
    parser.add_argument('-e', '--epochs', type=int, help='1', default=1)
    parser.add_argument('-is_wandb', '--is_wandb', type=bool, default=False)
    parser.add_argument('-max_n_train_example', '--max_n_train_example', type=int, default=None)
    parser.add_argument('-max_n_eval_example', '--max_n_eval_example', type=int, default=None)
    parser.add_argument(
        '-type', '--intervention_type', type=str, 
        help='LearnedSourceLowRankRotatedSpaceIntervention', default="LearnedSourceLowRankRotatedSpaceIntervention")
    parser.add_argument('-gradient_accumulation_steps', '--gradient_accumulation_steps', type=int, default=4)
    parser.add_argument('-batch_size', '--batch_size', type=int, default=4)
    parser.add_argument('-output_dir', '--output_dir', type=str, default="./official_results")
    
    args = parser.parse_args()

    model = args.model
    layers = args.layers
    rank = args.rank
    position = args.position
    epochs = args.epochs
    seed = args.seed
    intervention_type = args.intervention_type
    set_seed(seed)
    max_n_train_example = args.max_n_train_example
    max_n_eval_example = args.max_n_eval_example
    is_wandb = args.is_wandb
    gradient_accumulation_steps = args.gradient_accumulation_steps
    batch_size = args.batch_size
    output_dir = args.output_dir
    
    print(
        f"model: {model}, intervention_type: {intervention_type}"
        f"layers: {layers}, rank: {rank}, "
        f"position: {position}, epoch: {epochs}"
    )
    run_name = f"math.intervention_type={intervention_type}.layers={layers}."\
               f"rank={rank}.position={position}.epoch={epochs}"
    
    config, _, llama = create_llama(model)
    _ = llama.to(device)
    _ = llama.eval()
    
    # post-processing the inputs
    if intervention_type == "LearnedSourceLowRankRotatedSpaceIntervention":
        intervention_type = LearnedSourceLowRankRotatedSpaceIntervention
    elif intervention_type == "ConditionedSourceLowRankRotatedSpaceIntervention":
        intervention_type = ConditionedSourceLowRankRotatedSpaceIntervention
    user_give_all_layers = False
    if layers != "all":
        if "+" in layers:
            parsed_layers = []
            for l in layers.split("+"):
                for ll in l.split(";"):
                    parsed_layers += [int(ll)]
            user_give_all_layers = True
            layers = parsed_layers
        else:
            layers = [int(l) for l in layers.split(";")]
    else:
        layers = [l for l in range(config.num_hidden_layers)]
    assert position in {"last", "last+first", "first+last"}
    if position in {"last+first", "first+last"}:
        if user_give_all_layers:
            pass
        else:
            layers += layers
    
    tokenizer = AutoTokenizer.from_pretrained(model)
    tokenizer.padding_side = "right" 
    special_tokens_dict = dict()
    if tokenizer.pad_token is None:
        special_tokens_dict["pad_token"] = DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        special_tokens_dict["eos_token"] = DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        special_tokens_dict["bos_token"] = DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        special_tokens_dict["unk_token"] = DEFAULT_UNK_TOKEN
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    llama.resize_token_embeddings(len(tokenizer))
    print("adding new tokens count: ", num_new_tokens)

    task = "math_10k"
    
    ###################
    # data loaders
    ###################
    all_base_input_ids, all_base_positions, all_output_ids, all_source_input_ids = [], [], [], []

    task_dataset = jload(f"./datasets/{task}.json")
    for data_item in task_dataset[:max_n_train_example]:
        assert data_item['input'] == ""
        base_prompt = prompt_template % data_item['instruction'].strip()
        # base input = base prompt + steered base output
        base_input = base_prompt + data_item["output"].strip() + tokenizer.pad_token
        base_prompt_length = len(tokenizer(
            # we use 256 to follow previous work's cut-off length
            base_prompt, max_length=256, truncation=True, return_tensors="pt")["input_ids"][0])
        base_input_ids = tokenizer(
            base_input, max_length=256, truncation=True, return_tensors="pt")["input_ids"][0]
        output_ids = tokenizer(
            base_input, max_length=256, truncation=True, return_tensors="pt")["input_ids"][0]
        output_ids[:base_prompt_length] = -100
        base_input_ids[-1] = tokenizer.pad_token_id
        output_ids[-1] = tokenizer.pad_token_id
    
        all_base_input_ids.append(base_input_ids)
        all_base_positions.append([base_prompt_length-1]) # intervene on the last prompt token
        all_output_ids.append(output_ids)
            
    raw_train = (
        all_base_input_ids,
        all_base_positions,
        all_output_ids,
    )
    
    train_dataset = Dataset.from_dict(
        {
            "input_ids": raw_train[0],
            "intervention_position": raw_train[1],
            "labels": raw_train[2],
        }
    ).shuffle(seed=seed)
    if max_n_train_example is not None:
        train_dataset = train_dataset.select(range(max_n_train_example))
    
    data_collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        model=llama,
        label_pad_token_id=-100,
        padding="longest"
    )
    
    initial_lr = 3e-4
    total_step = 0

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, batch_size=batch_size, collate_fn=data_collator)
    
    config = IntervenableConfig([{
        "layer": l,
        "component": "block_output",
        "low_rank_dimension": rank} for l in layers],
        intervention_type
    )
    intervenable = IntervenableModel(config, llama)
    intervenable.set_device(device)
    intervenable.disable_model_gradients()
    
    n_params = intervenable.count_parameters()
    
    optimizer = torch.optim.Adam(
        intervenable.get_trainable_parameters(), lr=initial_lr
    )
    scheduler = torch.optim.lr_scheduler.LinearLR(
        optimizer, end_factor=0.1, total_iters=epochs
    )
    intervenable.model.train()  # train enables drop-off but no grads
    print("llama trainable parameters: ", count_parameters(intervenable.model))
    print("intervention trainable parameters: ", n_params)
    
    if is_wandb:
        run = wandb.init(
            project="Steer_LM", 
            entity="wuzhengx",
            name=run_name,
        )
        wandb.log({"train/n_params": n_params})
                
    train_iterator = trange(0, int(epochs), desc="Epoch")
    for epoch in train_iterator:
        epoch_iterator = tqdm(
            train_dataloader, desc=f"Epoch: {epoch}", position=0, leave=True
        )
        for step, inputs in enumerate(epoch_iterator):
            for k, v in inputs.items():
                if v is not None and isinstance(v, torch.Tensor):
                    inputs[k] = v.to(device)
            b_s = inputs["input_ids"].shape[0]
            base_unit_location = inputs["intervention_position"].tolist()
            if position in {"last+first", "first+last"}:
                base_first_token = torch.zeros_like(inputs["intervention_position"]).tolist()
                intervention_locations = [base_first_token]*(len(layers)//2)+[base_unit_location]*(len(layers)//2)
            else:
                intervention_locations = [base_unit_location]*len(layers)
                
            _, cf_outputs = intervenable(
                {"input_ids": inputs["input_ids"]},
                unit_locations={"sources->base": (None, intervention_locations)})

            # lm loss on counterfactual labels
            lm_logits = cf_outputs.logits
            labels = inputs["labels"]
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
            loss_str = round(loss.item(), 2)
            epoch_iterator.set_postfix({"loss": loss_str})
            if is_wandb:
                wandb.log({"train/loss": loss_str})
            if gradient_accumulation_steps > 1:
                loss = loss / gradient_accumulation_steps
            loss.backward()
            if total_step % gradient_accumulation_steps == 0:
                if not (gradient_accumulation_steps > 1 and total_step == 0):
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
            total_step += 1
    
    # avoid saving for now
    # intervenable.save(save_directory=f"./official_results/{run_name}")
    create_directory(f"{output_dir}/{run_name}")
    
    args_dict = vars(args)
    json_file_name = f"{output_dir}/{run_name}/args.json"
    with open(json_file_name, 'w') as json_file:
        json.dump(args_dict, json_file, indent=4)
    
    
    eval_results = {}
    eval_tasks = [
        "MultiArith", "gsm8k", "AddSub", "AQuA", "SingleEq", "SVAMP"
    ]
    
    for i in range(len(eval_tasks)):
        eval_dataset = jload(f"./datasets/{eval_tasks[i]}/test.json")
        if max_n_eval_example is not None:
            max_sample_examples = min(max_n_eval_example, len(eval_dataset))
            sampled_items = random.sample(range(len(eval_dataset)), max_sample_examples)
            sampled_eval_dataset = [eval_dataset[idx] for idx in sampled_items]
        else:
            sampled_eval_dataset = eval_dataset
        correct_count = 0
        totol_count = 0
        eval_iterator = tqdm(
            sampled_eval_dataset, position=0, leave=True
        )
        generations = []
        for data_item in eval_iterator:
            prompt = prompt_template % data_item['instruction'].strip()
            # base input = base prompt + steered base output
            answer = data_item["answer"].strip()
            prompt = tokenizer(prompt, return_tensors="pt").to(device)
            
            if position in {"last+first", "first+last"}:
                base_unit_location = prompt["input_ids"].shape[-1] - 1 
                base_unit_location = {"sources->base": (None, [[[0]]]*(len(layers)//2) + [[[base_unit_location]]]*(len(layers)//2))}
            else:
                base_unit_location = prompt["input_ids"].shape[-1] - 1 
                base_unit_location = {"base": base_unit_location}
            
            _, steered_response = intervenable.generate(
                prompt, 
                unit_locations=base_unit_location,
                intervene_on_prompt=True,
                # since we are generating the CoT, we follow previous works setting.
                temperature=0.1,
                top_p=0.75,
                top_k=40,
                num_beams=4,
                max_new_tokens=128,
                do_sample=True,
            )
            raw_generation = tokenizer.decode(steered_response[0], skip_special_tokens=True)
            generation = raw_generation.split(trigger_tokens)[-1]

            generation = generation.strip()
            if eval_tasks[i] == "AQuA":
                generation = extract_answer_letter(generation)
                if generation.strip() == answer.strip():
                    correct_count += 1
            else:
                generation = extract_answer_number(generation)
                if generation == float(answer):
                    correct_count += 1
            totol_count += 1
            metric_str = round(correct_count/totol_count, 3)
            eval_iterator.set_postfix({"em": metric_str})
            generations += [{
                "instruction": data_item['instruction'].strip(),
                "input": data_item['input'].strip(),
                "raw_generation": raw_generation,
                "generation": generation,
                "answer": answer
            }]
        if is_wandb:
            wandb.log({f"eval/{eval_tasks[i]}": metric_str})
        eval_results[eval_tasks[i]] = metric_str
        result_json_file_name = f"{output_dir}/{run_name}/{eval_tasks[i]}_outputs.json"
        with open(result_json_file_name, 'w') as json_file:
            json.dump(generations, json_file, indent=4)
            
    result_json_file_name = f"{output_dir}/{run_name}/eval_results.json"
    eval_results["n_params"] = n_params
    with open(result_json_file_name, 'w') as json_file:
        json.dump(eval_results, json_file, indent=4)
    
if __name__ == "__main__":
    main()