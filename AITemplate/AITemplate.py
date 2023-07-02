import os
import sys
import comfy.model_management
import comfy.samplers
import comfy.sample
import comfy.utils
import comfy.sd
import comfy.k_diffusion.external as k_diffusion_external
from comfy.model_management import vram_state as vram_st
# so we can import nodes and latent_preview
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", ".."))
import nodes
import latent_preview
import torch
import contextlib
import sys
import time
import tempfile
import math

from .ait.inference import AITemplateModelWrapper
from .ait import AIT
from .ait.inference import clip_inference, unet_inference, vae_inference, controlnet_inference

MAX_RESOLUTION=8192

def cleanup_temp_library(prefix="ait", extension=".so"):
    temp_dir = tempfile.gettempdir()
    dir_list = os.listdir(temp_dir)
    dir_list = [x for x in dir_list if x.startswith(prefix) and x.endswith(extension)]
    for x in dir_list:
        try:
            os.remove(os.path.join(temp_dir, x))
        except:
            pass

cleanup_temp_library(prefix="", extension=".so")

supported_ait_extensions = set(['.so', '.xz'])
base_path = os.path.dirname(os.path.realpath(__file__))
modules_dir = os.path.join(base_path, "modules")
folder_names_and_paths = {}
folder_names_and_paths["aitemplate"] = ([modules_dir], supported_ait_extensions)
filename_list_cache = {}
current_loaded_model = None
vram_state = None

modules_path = str(modules_dir).replace("\\", "/")
AITemplate = AIT(modules_path)
AIT_OS = "windows" if os.name == "nt" else "linux"
cuda = torch.cuda.get_device_capability()
if cuda[0] == 7 and cuda[1] == 5:
    AIT_CUDA = "sm75"
elif cuda[0] == 7 and cuda[1] == 0:
    AIT_CUDA = "sm70"
elif cuda[0] >= 8:
    AIT_CUDA = "sm80"
else:
    raise ValueError(f"Unsupported CUDA version {cuda[0]}.{cuda[1]}")


def get_full_path(folder_name, filename):
    global folder_names_and_paths
    if folder_name not in folder_names_and_paths:
        return None
    folders = folder_names_and_paths[folder_name]
    filename = os.path.relpath(os.path.join("/", filename), "/")
    for x in folders[0]:
        full_path = os.path.join(x, filename)
        if os.path.isfile(full_path):
            return full_path

    return None

def recursive_search(directory):
    if not os.path.isdir(directory):
        return [], {}
    result = []
    dirs = {directory: os.path.getmtime(directory)}
    for root, subdir, file in os.walk(directory, followlinks=True):
        for filepath in file:
            #we os.path,join directory with a blank string to generate a path separator at the end.
            result.append(os.path.join(root, filepath).replace(os.path.join(directory,''),''))
        for d in subdir:
            path = os.path.join(root, d)
            dirs[path] = os.path.getmtime(path)
    return result, dirs

def filter_files_extensions(files, extensions):
    return sorted(list(filter(lambda a: os.path.splitext(a)[-1].lower() in extensions, files)))


def filter_files_contains(files, contains):
    for x in contains:
        files = list(filter(lambda a: x in a, files))
    return sorted(files)

def get_filename_list_(folder_name):
    global folder_names_and_paths
    output_list = set()
    folders = folder_names_and_paths[folder_name]
    output_folders = {}
    for x in folders[0]:
        files, folders_all = recursive_search(x)
        output_list.update(filter_files_extensions(files, folders[1]))
        output_folders = {**output_folders, **folders_all}

    return (sorted(list(output_list)), output_folders, time.perf_counter())

def cached_filename_list_(folder_name):
    global filename_list_cache
    global folder_names_and_paths
    if folder_name not in filename_list_cache:
        return None
    out = filename_list_cache[folder_name]
    if time.perf_counter() < (out[2] + 0.5):
        return out
    for x in out[1]:
        time_modified = out[1][x]
        folder = x
        if os.path.getmtime(folder) != time_modified:
            return None

    folders = folder_names_and_paths[folder_name]
    for x in folders[0]:
        if os.path.isdir(x):
            if x not in out[1]:
                return None

    return out

def get_filename_list(folder_name):
    global filename_list_cache
    out = cached_filename_list_(folder_name)
    if out is None:
        out = get_filename_list_(folder_name)
        filename_list_cache[folder_name] = out
    return list(out[0])


def common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent, denoise=1.0, disable_noise=False, start_step=None, last_step=None, force_full_denoise=False):
    use_aitemplate = isinstance(model, tuple)
    if use_aitemplate:
        model, keep_loaded = model
    device = comfy.model_management.get_torch_device()
    latent_image = latent["samples"]

    if disable_noise:
        noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
    else:
        batch_inds = latent["batch_index"] if "batch_index" in latent else None
        noise = comfy.sample.prepare_noise(latent_image, seed, batch_inds)

    noise_mask = None
    if "noise_mask" in latent:
        noise_mask = latent["noise_mask"]

    preview_format = "JPEG"
    if preview_format not in ["JPEG", "PNG"]:
        preview_format = "JPEG"

    previewer = latent_preview.get_previewer(device, model.model.latent_format)

    if use_aitemplate:
        model = model, keep_loaded

    pbar = comfy.utils.ProgressBar(steps)
    def callback(step, x0, x, total_steps):
        preview_bytes = None
        if previewer:
            preview_bytes = previewer.decode_latent_to_preview_image(preview_format, x0)
        pbar.update_absolute(step + 1, total_steps, preview_bytes)

    samples = comfy.sample.sample(model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image,
                                  denoise=denoise, disable_noise=disable_noise, start_step=start_step, last_step=last_step,
                                  force_full_denoise=force_full_denoise, noise_mask=noise_mask, callback=callback, seed=seed)
    out = latent.copy()
    out["samples"] = samples
    return (out, )

nodes.common_ksampler = common_ksampler 

def maximum_batch_area():
    global vram_state

    memory_free = comfy.model_management.get_free_memory() / (1024 * 1024)
    if comfy.model_management.xformers_enabled() or comfy.model_management.pytorch_attention_flash_attention():
        area = 200 * memory_free
    else:
        #TODO: this formula is because AMD sucks and has memory management issues which might be fixed in the future
        area = ((memory_free - 1024) * 0.9) / (0.6)
    return int(max(area, 0))

comfy.model_management.maximum_batch_area = maximum_batch_area

def load_additional_models(positive, negative):
    """loads additional models in positive and negative conditioning"""
    control_nets = comfy.sample.get_models_from_cond(positive, "control") + comfy.sample.get_models_from_cond(negative, "control")
    gligen = comfy.sample.get_models_from_cond(positive, "gligen") + comfy.sample.get_models_from_cond(negative, "gligen")
    gligen = [x[1] for x in gligen]
    models = control_nets + gligen
    return models


def sample(model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=1.0, disable_noise=False, start_step=None, last_step=None, force_full_denoise=False, noise_mask=None, sigmas=None, callback=None, disable_pbar=False, seed=None):
    global current_loaded_model
    global vram_state
    global AITemplate
    global vram_st
    use_aitemplate = isinstance(model, tuple)
    if use_aitemplate:
        model, keep_loaded = model
        device = torch.device("cpu")
    else:
        device = comfy.model_management.get_torch_device()

    has_loaded = False
    if use_aitemplate:
        context_dim = -1
        control = False
        for pos in positive:
            for x in pos:
                if type(x) is dict:
                    if "control" in x:
                        control = True
                        break
                else:
                    context_dim = x.shape[2]
        for neg in negative:
            for x in neg:
                if type(x) is dict:
                    if "control" in x:
                        control = True
                        break
        sd = "v1"
        if context_dim == 1024:
            sd = "v2"
        batch_size = noise.shape[0]
        resolution = max(noise.shape[2], noise.shape[3]) * 8
        model_type = "unet"
        if control:
            model_type = "control_unet"
        module = AITemplate.loader.filter_modules(AIT_OS, sd, AIT_CUDA, batch_size, resolution, model_type)[0]
        if keep_loaded == "disable":
            if len(AITemplate.unet.keys()) > 0:
                to_delete = list(AITemplate.unet.keys())
                for x in to_delete:
                    del AITemplate.unet[x]
        if module['sha256'] not in AITemplate.unet:
            AITemplate.unet[module['sha256']] = AITemplate.loader.load_module(module['sha256'], module['url'])
            has_loaded = True

    if noise_mask is not None:
        noise_mask = comfy.sample.prepare_mask(noise_mask, noise.shape, device)

    if use_aitemplate:
        apply_aitemplate_weights = has_loaded or current_loaded_model != model or keep_loaded == "disable"
        try:
            model.patch_model()
        except Exception as e:
            model.unpatch_model()
            raise e
    else:
        comfy.model_management.load_model_gpu(model)
    real_model = model.model

    if use_aitemplate:
        current_loaded_model = model
        real_model.alphas_cumprod = real_model.alphas_cumprod.float()
        if apply_aitemplate_weights:
            AITemplate.unet[module['sha256']] = AITemplate.loader.apply_unet(
                aitemplate_module=AITemplate.unet[module['sha256']],
                unet=AITemplate.loader.compvis_unet(real_model.state_dict()),
                in_channels=real_model.diffusion_model.in_channels,
                conv_in_key="conv_in_weight",
            )

    noise = noise.to(device)
    latent_image = latent_image.to(device)

    positive_copy = comfy.sample.broadcast_cond(positive, noise.shape[0], device)
    negative_copy = comfy.sample.broadcast_cond(negative, noise.shape[0], device)

    models = load_additional_models(positive, negative)

    sampler = comfy.samplers.KSampler(real_model, steps=steps, device=device, sampler=sampler_name, scheduler=scheduler, denoise=denoise, model_options=model.model_options)
    if use_aitemplate:
        model_wrapper = AITemplateModelWrapper(AITemplate.unet[module['sha256']], real_model.alphas_cumprod)
        sampler.model_denoise = comfy.samplers.CFGNoisePredictor(model_wrapper)
        if real_model.parameterization == "v":
            sampler.model_wrap = comfy.samplers.CompVisVDenoiser(sampler.model_denoise, quantize=True)
        else:
            sampler.model_wrap = k_diffusion_external.CompVisDenoiser(sampler.model_denoise, quantize=True)
        sampler.model_wrap.parameterization = sampler.model.parameterization
        sampler.model_k = comfy.samplers.KSamplerX0Inpaint(sampler.model_wrap)

    samples = sampler.sample(noise, positive_copy, negative_copy, cfg=cfg, latent_image=latent_image, start_step=start_step, last_step=last_step, force_full_denoise=force_full_denoise, denoise_mask=noise_mask, sigmas=sigmas, callback=callback, disable_pbar=disable_pbar, seed=seed)
    samples = samples.cpu()

    comfy.sample.cleanup_additional_models(models)

    if use_aitemplate and keep_loaded == "disable":
        del AITemplate.unet[module['sha256']]
        del sampler
        controlnet_keys = list(AITemplate.controlnet.keys())
        for x in controlnet_keys:
            del AITemplate.controlnet[x]
        AITemplate.control_net = None
        torch.cuda.empty_cache()
        current_loaded_model = None

    return samples

comfy.sample.sample = sample



class ControlNet:
    def __init__(self, control_model, global_average_pooling=False, device=None):
        global AITemplate
        if AITemplate.control_net is not None:
            self.aitemplate = True
        else:
            self.aitemplate = None
        self.control_model = control_model
        self.cond_hint_original = None
        self.cond_hint = None
        self.strength = 1.0
        if device is None:
            device = comfy.model_management.get_torch_device()
        self.device = device
        self.previous_controlnet = None
        self.global_average_pooling = global_average_pooling

    def aitemplate_controlnet(
        self, latent_model_input, timesteps, encoder_hidden_states, controlnet_cond
    ):
        global AITemplate
        batch = latent_model_input.shape[0] / 2
        resolution = max(latent_model_input.shape[2], latent_model_input.shape[3]) * 8
        control_net_module = None
        if len(AITemplate.controlnet.keys()) == 0:
            module = AITemplate.loader.filter_modules(AIT_OS, "v1", AIT_CUDA, batch, resolution, "controlnet")[0]
            AITemplate.controlnet[module['sha256']] = AITemplate.loader.load_module(module['sha256'], module['url'])
            AITemplate.controlnet[module['sha256']] = AITemplate.loader.apply_controlnet(
                aitemplate_module=AITemplate.controlnet[module['sha256']],
                controlnet=AITemplate.loader.compvis_controlnet(self.control_model.state_dict())
            )
            control_net_module = module['sha256']
        else:
            control_net_module = list(AITemplate.controlnet.keys())[0]
        if self.aitemplate is None:
            raise RuntimeError("No aitemplate loaded")
        return controlnet_inference(
            exe_module=AITemplate.controlnet[control_net_module],
            latent_model_input=latent_model_input,
            timesteps=timesteps,
            encoder_hidden_states=encoder_hidden_states,
            controlnet_cond=controlnet_cond,
        )

    def get_control(self, x_noisy, t, cond, batched_number):
        control_prev = None
        if self.previous_controlnet is not None:
            control_prev = self.previous_controlnet.get_control(x_noisy, t, cond, batched_number)

        output_dtype = x_noisy.dtype
        if self.cond_hint is None or x_noisy.shape[2] * 8 != self.cond_hint.shape[2] or x_noisy.shape[3] * 8 != self.cond_hint.shape[3]:
            if self.cond_hint is not None:
                del self.cond_hint
            self.cond_hint = None
            self.cond_hint = comfy.utils.common_upscale(self.cond_hint_original, x_noisy.shape[3] * 8, x_noisy.shape[2] * 8, 'nearest-exact', "center").to(self.control_model.dtype).to(self.device)
        if x_noisy.shape[0] != self.cond_hint.shape[0]:
            self.cond_hint = comfy.sd.broadcast_image_to(self.cond_hint, x_noisy.shape[0], batched_number)
        if self.aitemplate is None:
            if self.control_model.dtype == torch.float16:
                precision_scope = torch.autocast
            else:
                precision_scope = contextlib.nullcontext

            with precision_scope(comfy.model_management.get_autocast_device(self.device)):
                self.control_model = comfy.model_management.load_if_low_vram(self.control_model)
                context = torch.cat(cond['c_crossattn'], 1)
                y = cond.get('c_adm', None)
                control = self.control_model(x=x_noisy, hint=self.cond_hint, timesteps=t, context=context, y=y)
                self.control_model = comfy.model_management.unload_if_low_vram(self.control_model)
        else:
            control = self.aitemplate_controlnet(x_noisy, t, cond, self.cond_hint)
        out = {'middle':[], 'output': []}
        autocast_enabled = torch.is_autocast_enabled()

        for i in range(len(control)):
            if i == (len(control) - 1):
                key = 'middle'
                index = 0
            else:
                key = 'output'
                index = i
            x = control[i]
            if self.global_average_pooling:
                x = torch.mean(x, dim=(2, 3), keepdim=True).repeat(1, 1, x.shape[2], x.shape[3])

            x *= self.strength
            if x.dtype != output_dtype and not autocast_enabled:
                x = x.to(output_dtype)

            if control_prev is not None and key in control_prev:
                prev = control_prev[key][index]
                if prev is not None:
                    x += prev
            out[key].append(x)
        if control_prev is not None and 'input' in control_prev:
            out['input'] = control_prev['input']
        return out

    def set_cond_hint(self, cond_hint, strength=1.0):
        self.cond_hint_original = cond_hint
        self.strength = strength
        return self

    def set_previous_controlnet(self, controlnet):
        self.previous_controlnet = controlnet
        return self

    def cleanup(self):
        if self.previous_controlnet is not None:
            self.previous_controlnet.cleanup()
        if self.cond_hint is not None:
            del self.cond_hint
            self.cond_hint = None

    def copy(self):
        c = ControlNet(self.control_model)
        c.cond_hint_original = self.cond_hint_original
        c.strength = self.strength
        return c

    def get_models(self):
        out = []
        if self.previous_controlnet is not None:
            out += self.previous_controlnet.get_models()
        out.append(self.control_model)
        return out

comfy.sd.ControlNet = ControlNet

class AITemplateLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "model": ("MODEL",),
                              "keep_loaded": (["enable", "disable"], ),
                              }}
    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_aitemplate"

    CATEGORY = "loaders"

    def load_aitemplate(self, model, keep_loaded):
        return ((model,keep_loaded),)



class AITemplateVAEEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { 
            "pixels": ("IMAGE", ),
            "vae": ("VAE", ),
            "keep_loaded": (["enable", "disable"], ),
        }}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "encode"

    CATEGORY = "latent"

    @staticmethod
    def vae_encode_crop_pixels(pixels):
        x = (pixels.shape[1] // 8) * 8
        y = (pixels.shape[2] // 8) * 8
        if pixels.shape[1] != x or pixels.shape[2] != y:
            x_offset = (pixels.shape[1] % 8) // 2
            y_offset = (pixels.shape[2] % 8) // 2
            pixels = pixels[:, x_offset:x + x_offset, y_offset:y + y_offset, :]
        return pixels

    def encode(self, vae, pixels, keep_loaded):
        global AITemplate
        resolution = max(pixels.shape[1], pixels.shape[2])
        model_type = "vae_encode"
        if keep_loaded == "disable":
            if len(AITemplate.vae.keys()) > 0:
                to_delete = list(AITemplate.vae.keys())
                for key in to_delete:
                    del AITemplate.vae[key]
        module = AITemplate.loader.filter_modules(AIT_OS, "v1", AIT_CUDA, 1, resolution, model_type)[0]
        if module["sha256"] not in AITemplate.vae:
            AITemplate.vae[module["sha256"]] = AITemplate.loader.load_module(module["sha256"], module["url"])
            AITemplate.vae[module["sha256"]] = AITemplate.loader.apply_vae(
                aitemplate_module=AITemplate.vae[module["sha256"]],
                vae=AITemplate.loader.compvis_vae(vae.first_stage_model.state_dict()),
                encoder=True,
            )
        pixels = self.vae_encode_crop_pixels(pixels)
        pixels = pixels[:,:,:,:3]
        pixels = pixels.movedim(-1, 1)
        pixels = 2. * pixels - 1.
        samples = vae_inference(AITemplate.vae[module["sha256"]], pixels, encoder=True)
        samples = samples.cpu()
        if keep_loaded == "disable":
            del AITemplate.vae[module["sha256"]]
            torch.cuda.empty_cache()
        return ({"samples":samples}, )



class VAEEncodeForInpaint:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { 
            "pixels": ("IMAGE", ),
            "vae": ("VAE", ),
            "mask": ("MASK", ),
            "grow_mask_by": ("INT", {"default": 6, "min": 0, "max": 64, "step": 1}),
            "keep_loaded": (["enable", "disable"], ),
        }}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "encode"

    CATEGORY = "latent/inpaint"

    def encode(self, vae, pixels, mask, keep_loaded, grow_mask_by=6):
        global AITemplate
        resolution = max(pixels.shape[1], pixels.shape[2])
        model_type = "vae_encode"
        if keep_loaded == "disable":
            if len(AITemplate.vae.keys()) > 0:
                to_delete = list(AITemplate.vae.keys())
                for key in to_delete:
                    del AITemplate.vae[key]
        module = AITemplate.loader.filter_modules(AIT_OS, "v1", AIT_CUDA, 1, resolution, model_type)[0]
        if module["sha256"] not in AITemplate.vae:
            AITemplate.vae[module["sha256"]] = AITemplate.loader.load_module(module["sha256"], module["url"])
            AITemplate.vae[module["sha256"]] = AITemplate.loader.apply_vae(
                aitemplate_module=AITemplate.vae[module["sha256"]],
                vae=AITemplate.loader.compvis_vae(vae.first_stage_model.state_dict()),
                encoder=True,
            )
        x = (pixels.shape[1] // 8) * 8
        y = (pixels.shape[2] // 8) * 8
        mask = torch.nn.functional.interpolate(mask.reshape((-1, 1, mask.shape[-2], mask.shape[-1])), size=(pixels.shape[1], pixels.shape[2]), mode="bilinear")

        pixels = pixels.clone()
        if pixels.shape[1] != x or pixels.shape[2] != y:
            x_offset = (pixels.shape[1] % 8) // 2
            y_offset = (pixels.shape[2] % 8) // 2
            pixels = pixels[:,x_offset:x + x_offset, y_offset:y + y_offset,:]
            mask = mask[:,:,x_offset:x + x_offset, y_offset:y + y_offset]

        #grow mask by a few pixels to keep things seamless in latent space
        if grow_mask_by == 0:
            mask_erosion = mask
        else:
            kernel_tensor = torch.ones((1, 1, grow_mask_by, grow_mask_by))
            padding = math.ceil((grow_mask_by - 1) / 2)

            mask_erosion = torch.clamp(torch.nn.functional.conv2d(mask.round(), kernel_tensor, padding=padding), 0, 1)

        m = (1.0 - mask.round()).squeeze(1)
        for i in range(3):
            pixels[:,:,:,i] -= 0.5
            pixels[:,:,:,i] *= m
            pixels[:,:,:,i] += 0.5
        pixels = pixels[:,:,:,:3]
        pixels = pixels.movedim(-1, 1)
        pixels = 2. * pixels - 1.
        samples = vae_inference(AITemplate.vae[module["sha256"]], pixels, encoder=True)
        samples = samples.cpu()
        if keep_loaded == "disable":
            del AITemplate.vae[module["sha256"]]
            torch.cuda.empty_cache()
        return ({"samples":samples, "noise_mask": (mask_erosion[:,:,:x,:y].round())}, )


class AITemplateVAEDecode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    { 
                    "vae": ("VAE",),
                    "keep_loaded": (["enable", "disable"], ),
                    "samples": ("LATENT", ), "vae": ("VAE", )
                    }
                }
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "decode"

    CATEGORY = "latent"

    def decode(self, vae, keep_loaded, samples):
        global AITemplate
        resolution = max(samples["samples"].shape[2], samples["samples"].shape[3]) * 8
        model_type = "vae"
        module = AITemplate.loader.filter_modules(AIT_OS, "v1", AIT_CUDA, 1, resolution, model_type)[0]
        if keep_loaded == "disable":
            if len(AITemplate.vae.keys()) > 0:
                to_delete = list(AITemplate.vae.keys())
                for key in to_delete:
                    del AITemplate.vae[key]
        if module["sha256"] not in AITemplate.vae:
            AITemplate.vae[module["sha256"]] = AITemplate.loader.load_module(module["sha256"], module["url"])
            AITemplate.vae[module["sha256"]] = AITemplate.loader.apply_vae(
                aitemplate_module=AITemplate.vae[module["sha256"]],
                vae=AITemplate.loader.compvis_vae(vae.first_stage_model.state_dict()),
            )
        output = (torch.clamp((vae_inference(AITemplate.vae[module["sha256"]], samples["samples"]) + 1.0) / 2.0, min=0.0, max=1.0).cpu().movedim(1,-1), )
        if keep_loaded == "disable":
            del AITemplate.vae[module["sha256"]]
            torch.cuda.empty_cache()
        return output


class AITemplateControlNetLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "control_net": ("CONTROL_NET",),
                                "keep_loaded": (["enable", "disable"], )
                              }}
    RETURN_TYPES = ("CONTROL_NET",)
    FUNCTION = "load_aitemplate_controlnet"

    CATEGORY = "loaders"

    def load_aitemplate_controlnet(self, control_net, keep_loaded):
        global AITemplate
        AITemplate.control_net = keep_loaded
        control_net.control_model = control_net.control_model.to("cpu")
        control_net.device = torch.device("cpu")
        torch.cuda.empty_cache()
        return (control_net,)

class AITemplateEmptyLatentImage:
    def __init__(self, device="cpu"):
        self.device = device

    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "width": ("INT", {"default": 512, "min": 64, "max": MAX_RESOLUTION, "step": 64}),
                              "height": ("INT", {"default": 512, "min": 64, "max": MAX_RESOLUTION, "step": 64}),
                              "batch_size": ("INT", {"default": 1, "min": 1, "max": 64})}}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "generate"

    CATEGORY = "latent"

    def generate(self, width, height, batch_size=1, latent_channels=4, down_factor=8):
        latent = torch.zeros([batch_size, latent_channels, height // down_factor, width // down_factor])
        return ({"samples":latent}, )


class AITemplateLatentUpscale:
    upscale_methods = ["nearest-exact", "bilinear", "area", "bicubic", "bislerp"]
    crop_methods = ["disabled", "center"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "samples": ("LATENT",), "upscale_method": (s.upscale_methods,),
                              "width": ("INT", {"default": 512, "min": 64, "max": MAX_RESOLUTION, "step": 64}),
                              "height": ("INT", {"default": 512, "min": 64, "max": MAX_RESOLUTION, "step": 64}),
                              "crop": (s.crop_methods,)}}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "upscale"

    CATEGORY = "latent"

    def upscale(self, samples, upscale_method, width, height, crop, down_factor=8):
        s = samples.copy()
        s["samples"] = comfy.utils.common_upscale(samples["samples"], width // down_factor, height // down_factor, upscale_method, crop)
        return (s,)
