from random import choices
from matplotlib.style import available
import torch
import csv
import math
import gradio as gr
import os.path
from pprint import pprint
import modules.ui
import ldm.modules.attention as atm
from modules import shared,scripts
from modules.processing import Processed,paths

#'"name","mode","divide ratios,"use base","baseratios","usecom","usencom",\n'
"""
SBM mod: Two dimensional regions (of variable size, NOT a matrix).
- Adds keywords ADDROW, ADDCOL and respective delimiters for aratios.
- A/bratios become list dicts: Inner dict of cols (varying length list) + start/end + number of breaks,
  outer layer is rows list.
  First value in each row is the row's ratio, the rest are col ratios.
  This fits prompts going left -> right, top -> down. 
- Unrelated BREAKS are counted per cell, and later extracted as multiple context indices.
- Each layer is cut up by both row + col ratios.
- Style improvements: Created classes for rows + cells and functions for some of the splitting.
- Base prompt overhaul: Added keyword ADDBASE, when present will trigger "use_base" automatically;
  base is excluded from the main prompt for dim calcs; returned to start before hook (+ base break count);
  during hook, context index skips base break count + 1. Rest is applied normally.
- CONT: Currently, there is no way to specify cols first, eg 1st col:2 rows, 2nd col:1 row.
  This can be done technically by duping the prompt for row sections,
  but a better solution is to use horz/vert as rows first / cols first.
"""

PRESETS =[
    ["Vertical-3", "Vertical",'"1,1,1"',"","False","False","False"],
    ["Horizontal-3", "Horizontal",'"1,1,1"',"","False","False","False"],
    ["Horizontal-7", "Horizontal",'"1,1,1,1,1,1,1"',"0.2","True","False","False"],
]
# SBM Keywords and delimiters for region breaks, following matlab rules.
# BREAK keyword is now passed through,  
KEYROW = "ADDROW"
KEYCOL = "ADDCOL"
KEYBASE = "ADDBASE"
KEYBRK = "BREAK"
DELIMROW = ";"
DELIMCOL = ","
MATMODE = "Matrix"
TOKENS = 77
fidentity = lambda x: x
fcountbrk = lambda x: x.count(KEYBRK)
ffloat = lambda x: float(x)
fint = lambda x: int(x)
fspace = lambda x: " {} ".format(x)

class RegionCell():
    """Cell used to split a layer to single prompts."""
    def __init__(self, st, ed, base, breaks):
        """Range with start and end values, base weight and breaks count for context splitting."""
        self.st = st # Range for the cell (cols only).
        self.ed = ed
        self.base = base # How much of the base prompt is applied (difference).
        self.breaks = breaks # How many unrelated breaks the prompt contains.
        
class RegionRow():
    """Row containing cell refs and its own ratio range."""
    def __init__(self, st, ed, cols):
        """Range with start and end values, base weight and breaks count for context splitting."""
        self.st = st # Range for the row.
        self.ed = ed
        self.cols = cols # List of cells.

def split_l2(s, kr, kc, indsingles = False, fmap = fidentity, basestruct = None):
    """Split string to 2d list (ie L2) per row and col keys.
    
    The output is a list of lists, each of varying length.
    If a L2 basestruct is provided,
    will adhere to its structure using the following broadcast rules:
    - Basically matches row by row of base and new.
    - If a new row is shorter than base, the last value is repeated to fill the row.
    - If both are the same length, copied as is.
    - If new row is longer, then additional values will overflow to the next row.
      This might be unintended sometimes, but allows making all items col separated,
      then the new structure is simply adapted to the base structure.
    - If there are too many values in new, they will be ignored.
    - If there are too few values in new, the last one is repeated to fill base. 
    For mixed row + col ratios, singles flag is provided -
    will extract the first value of each row to a separate list,
    and output structure is (row L1,cell L2).
    There MUST be at least one value for row, one value for col when singles is on;
    to prevent errors, the row value is copied to col if it's alone (shouldn't affect results).
    Singles still respects base broadcast rules, and repeats its own last value.
    The fmap function is applied to each cell before insertion to L2.
    TODO: Needs to be a case insensitive split. Use re.split.
    """
    lret = []
    if basestruct is None:
        lrows = s.split(kr)
        lrows = [row.split(kc) for row in lrows]
        for r in lrows:
            cell = [fmap(x) for x in r]
            lret.append(cell)
        if indsingles:
            lsingles = [row[0] for row in lret]
            lcells = [row[1:] if len(row) > 1 else row for row in lret]
            lret = (lsingles,lcells)
    else:
        lrows = s.split(kr)
        r = 0
        lcells = []
        lsingles = []
        vlast = 1
        for row in lrows:
            row2 = row.split(kc)
            row2 = [fmap(x) for x in row2]
            vlast = row2[-1]
            indstop = False
            while not indstop:
                if (r >= len(basestruct) # Too many cell values, ignore.
                or (len(row2) == 0 and len(basestruct) > 0)): # Cell exhausted.
                    indstop = True
                if indsingles and not indstop: # Singles split.
                    lsingles.append(row2[0]) # Row ratio.
                    if len(row2) > 1:
                        row2 = row2[1:]
                if len(basestruct[r]) >= len(row2): # Repeat last value.
                    indstop = True
                    broadrow = row2 + [row2[-1]] * (len(basestruct[r]) - len(row2))
                    r = r + 1
                    lcells.append(broadrow)
                else: # Overfilled this row, cut and move to next.
                    broadrow = row2[:len(basestruct[r])]
                    row2 = row2[len(basestruct[r]):]
                    r = r + 1
                    lcells.append(broadrow)
        # If not enough new rows, repeat the last one for entire base, preserving structure.
        cur = len(lcells)
        while cur < len(basestruct):
            lcells.append([vlast] * len(basestruct[cur]))
            cur = cur + 1
        lret = lcells
        if indsingles:
            lsingles = lsingles + [lsingles[-1]] * (len(basestruct) - len(lsingles))
            lret = (lsingles,lcells)
    return lret

def is_l2(l):
    return isinstance(l[0],list) 

def l2_count(l):
    cnt = 0
    for row in l:
        cnt + cnt + len(row)
    return cnt

def list_percentify(l):
    """Convert each row in L2 to relative part of 100%. 
    
    Also works on L1, applying once globally.
    """
    lret = []
    if is_l2(l):
        for row in l:
            # row2 = [float(v) for v in row]
            row2 = [v / sum(row) for v in row]
            lret.append(row2)
    else:
        row = l[:]
        # row2 = [float(v) for v in row]
        row2 = [v / sum(row) for v in row]
        lret = row2
    return lret

def list_cumsum(l):
    """Apply cumsum to L2 per row, ie newl[n] = l[0:n].sum .
    
    Works with L1.
    Actually edits l inplace, idc.
    """
    lret = []
    if is_l2(l):
        for row in l:
            for (i,v) in enumerate(row):
                if i > 0:
                    row[i] = v + row[i - 1]
            lret.append(row)
    else:
        row = l[:]
        for (i,v) in enumerate(row):
            if i > 0:
                row[i] = v + row[i - 1]
        lret = row
    return lret

def list_rangify(l):
    """Merge every 2 elems in L2 to a range, starting from 0.  
    
    """
    lret = []
    if is_l2(l):
        for row in l:
            row2 = [0] + row
            row3 = []
            for i in range(len(row2) - 1):
                row3.append([row2[i],row2[i + 1]]) 
            lret.append(row3)
    else:
        row2 = [0] + l
        row3 = []
        for i in range(len(row2) - 1):
            row3.append([row2[i],row2[i + 1]]) 
        lret = row3
    return lret

def round_dim(x,y):
    """Return division of two numbers, rounding 0.5 up.
    
    Seems that dimensions which are exactly 0.5 are rounded up - see 680x488, second iter.
    A simple mod check should get the job done.
    If not, can always brute force the divisor with +-1 on each of h/w.
    """
    return x // y + (x % y >= y // 2)

def main_forward(module,x,context,mask):
    
    # Forward.
    h = module.heads

    q = module.to_q(x)
    context = atm.default(context, x)
    k = module.to_k(context)
    v = module.to_v(context)

    q, k, v = map(lambda t: atm.rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q, k, v))

    sim = atm.einsum('b i d, b j d -> b i j', q, k) * module.scale

    if atm.exists(mask):
        mask = atm.rearrange(mask, 'b ... -> b (...)')
        max_neg_value = -torch.finfo(sim.dtype).max
        mask = atm.repeat(mask, 'b j -> (b h) () j', h=h)
        sim.masked_fill_(~mask, max_neg_value)

    attn = sim.softmax(dim=-1)

    out = atm.einsum('b i j, b j d -> b i d', attn, v)
    out = atm.rearrange(out, '(b h) n d -> b n (h d)', h=h)
    out = module.to_out(out)
    
    return out
class Script(modules.scripts.Script):
    def __init__(self):
        self.mode = ""
        self.w = 0
        self.h = 0
        self.usebase = False
        self.aratios = []
        self.bratios = []
        self.divide = 0
        self.count = 0
        self.pn = True
        self.hr = False
        self.hr_scale = 0
        self.hr_w = 0
        self.hr_h = 0
        self.batch_size = 0
        self.orig_all_prompts = []
        self.orig_all_negative_prompts = []
        self.all_prompts = []
        self.all_negative_prompts = []
        self.imgcount = 0

    def title(self):
        return "Regional Prompter"

    def show(self, is_img2img):
        return modules.scripts.AlwaysVisible

    def ui(self, is_img2img):
        path_root = scripts.basedir()
        filepath = os.path.join(path_root,"scripts", "regional_prompter_presets.csv")

        presets=[]

        presets  = loadpresets(filepath)

        with gr.Accordion("Regional Prompter", open=False):
            with gr.Row():
                active = gr.Checkbox(value=False, label="Active",interactive=True,elem_id="RP_active")
            with gr.Row():
                mode = gr.Radio(label="Divide mode", choices=["Horizontal", "Vertical"], value="Horizontal",  type="value", interactive=True)
            with gr.Row(visible=True):
                ratios = gr.Textbox(label="Divide Ratio",lines=1,value="1,1",interactive=True,elem_id="RP_divide_ratio",visible=True)
                baseratios = gr.Textbox(label="Base Ratio", lines=1,value="0.2",interactive=True,  elem_id="RP_base_ratio", visible=True)
            with gr.Row():
                usebase = gr.Checkbox(value=False, label="Use base prompt",interactive=True, elem_id="RP_usebase")
                usecom = gr.Checkbox(value=False, label="Use common prompt",interactive=True,elem_id="RP_usecommon")
                usencom = gr.Checkbox(value=False, label="Use common negative prompt",interactive=True,elem_id="RP_usecommon")
            with gr.Row():
                debug = gr.Checkbox(value=False, label="debug", interactive=True, elem_id="RP_debug")

            with gr.Accordion("Presets",open = False):
                with gr.Row():
                    availablepresets = gr.Dropdown(label="Presets", choices=[pr[0] for pr in presets], type="index")
                    applypresets = gr.Button(value="Apply Presets",variant='primary',elem_id="RP_applysetting")
                with gr.Row():
                    presetname = gr.Textbox(label="Preset Name",lines=1,value="",interactive=True,elem_id="RP_preset_name",visible=True)
                    savesets = gr.Button(value="Save to Presets",variant='primary',elem_id="RP_savesetting")

            settings = [mode, ratios, baseratios, usebase, usecom, usencom]
        
        def setpreset(select):
            presets = loadpresets(filepath)
            preset = presets[select]
            preset = preset[1:]
            def booler(text):
                return text == "TRUE" or text == "true" or text == "True"
            preset[1],preset[2] = preset[1].replace('"',""),preset[2].replace('"',"")
            preset[3],preset[4],preset[5] = booler(preset[3]),booler(preset[4]),booler(preset[5])
            return [gr.update(value = pr) for pr in preset]

        applypresets.click(fn=setpreset, inputs = availablepresets, outputs=settings)
        savesets.click(fn=savepresets, inputs = [presetname,*settings],outputs=availablepresets)
                
        return [active, debug, mode, ratios, baseratios, usebase, usecom, usencom]

    def process(self, p, active, debug, mode, aratios, bratios, usebase, usecom, usencom):
        if active:
            savepresets("lastrun",mode, aratios, usebase, bratios, usecom, usencom)
            self.__init__()
            self.mode = mode
            # SBM matrix mode detection.
            if (KEYROW in p.prompt.upper() or KEYCOL in p.prompt.upper() or DELIMROW in aratios):
                self.mode = MATMODE
            self.w = p.width
            self.h = p.height
            self.batch_size = p.batch_size

            self.debug = debug
            self.usebase = usebase

            self.hr = p.enable_hr
            self.hr_w = (p.hr_resize_x if p.hr_resize_x > p.width else p.width * p.hr_scale)
            self.hr_h = (p.hr_resize_y if p.hr_resize_y > p.height else p.height * p.hr_scale)
            # SBM In matrix mode, the ratios are broken up 
            if self.mode == MATMODE:
                # The addrow/addcol syntax is better, cannot detect regular breaks without it.
                # In any case, the preferred method will anchor the L2 structure. 
                if (KEYBASE in p.prompt.upper()): # Designated base.
                    self.usebase = True
                    baseprompt = p.prompt.split(KEYBASE,1)[0]
                    mainprompt = p.prompt.split(KEYBASE,1)[1] 
                    self.basebreak = fcountbrk(baseprompt)
                elif usebase: # Get base by first break as usual.
                    baseprompt = p.prompt.split(KEYBRK,1)[0]
                    mainprompt = p.prompt.split(KEYBRK,1)[1]
                else:
                    baseprompt = ""
                    mainprompt = p.prompt
                if (KEYCOL in mainprompt.upper() or KEYROW in mainprompt.upper()):
                    breaks = mainprompt.count(KEYROW) + mainprompt.count(KEYCOL) + int(self.usebase)
                    # Prompt anchors, count breaks between special keywords.
                    lbreaks = split_l2(mainprompt, KEYROW, KEYCOL, fmap = fcountbrk)
                    # Standard ratios, split to rows and cols.
                    (aratios2r,aratios2) = split_l2(aratios, DELIMROW, DELIMCOL, 
                                                    indsingles = True, fmap = ffloat, basestruct = lbreaks)
                    # More like "bweights", applied per cell only.
                    bratios2 = split_l2(bratios, DELIMROW, DELIMCOL, fmap = ffloat, basestruct = lbreaks)
                else:
                    breaks = mainprompt.count(KEYBRK) + int(self.usebase)
                    (aratios2r,aratios2) = split_l2(aratios, DELIMROW, DELIMCOL, indsingles = True, fmap = ffloat)
                    # Cannot determine which breaks matter.
                    lbreaks = split_l2("0", KEYROW, KEYCOL, fmap = fint, basestruct = aratios2)
                    bratios2 = split_l2(bratios, DELIMROW, DELIMCOL, fmap = ffloat, basestruct = lbreaks)
                    # If insufficient breaks, try to broadcast prompt - a bit dumb.
                    breaks = fcountbrk(mainprompt)
                    lastprompt = mainprompt.rsplit(KEYBRK)[-1]
                    if l2_count(aratios2) > breaks: 
                        mainprompt = mainprompt + (fspace(KEYBRK) + lastprompt) * (l2_count(aratios2) - breaks) 
                
                # Change all splitters to breaks.
                aratios2 = list_percentify(aratios2)
                aratios2 = list_cumsum(aratios2)
                aratios = list_rangify(aratios2)
                aratios2r = list_percentify(aratios2r)
                aratios2r = list_cumsum(aratios2r)
                aratiosr = list_rangify(aratios2r)
                bratios = bratios2 
                
                # Merge various L2s to cells and rows.
                drows = []
                for r,_ in enumerate(lbreaks):
                    dcells = []
                    for c,_ in enumerate(lbreaks[r]):
                        d = RegionCell(aratios[r][c][0], aratios[r][c][1], bratios[r][c], lbreaks[r][c])
                        dcells.append(d)
                    drow = RegionRow(aratiosr[r][0], aratiosr[r][1], dcells)
                    drows.append(drow)
                self.aratios = drows
                # Convert all keys to breaks, and expand neg to fit.
                mainprompt = mainprompt.replace(KEYROW,KEYBRK) # Cont: Should be case insensitive.
                mainprompt = mainprompt.replace(KEYCOL,KEYBRK)
                p.prompt = mainprompt
                if self.usebase:
                    p.prompt = baseprompt + fspace(KEYBRK) + p.prompt 
                p.all_prompts = [p.prompt] * len(p.all_prompts)
                np = p.negative_prompt
                np.replace(KEYROW,KEYBRK)
                np.replace(KEYCOL,KEYBRK)
                np = np.split(KEYBRK)
                nbreaks = len(np) - 1
                if breaks >= nbreaks: # Repeating the first neg as in orig code.
                    np.extend([np[0]] * (breaks - nbreaks))
                else: # Cut off the excess negs.
                    np = np[0:breaks + 1]
                for i ,n in enumerate(np):
                    if n.isspace() or n =="":
                        np[i] = ","
                p.negative_prompt = fspace(KEYBRK).join(np)
                p.all_negative_prompts = [p.negative_prompt] * len(p.all_negative_prompts)
                self.handle = hook_forwards(self,p.sd_model.model.diffusion_model)
            else:
                self, p = promptdealer(self, p, aratios, bratios, usebase, usecom, usencom)
    
                self.handle = hook_forwards(self, p.sd_model.model.diffusion_model)
    
                self.pt, self.nt ,ppt,pnt= tokendealer(p)
    
                print(f"pos tokens : {ppt}, neg tokens : {pnt}")
                
                self.eq = True if len(self.pt) == len(self.nt) else False
        else:   
            if hasattr(self,"handle"):
                hook_forwards(self, p.sd_model.model.diffusion_model, remove=True)
                del self.handle


        return p

    def postprocess_image(self, p,pp, active, debug, mode, aratios, bratios, usebase, usecom, usencom):
        if active:
            if usecom:
                p.prompt = self.orig_all_prompt[0]
                p.all_prompts[self.imgcount] = self.orig_all_prompt[self.imgcount]  
            if usencom:
                p.negative_prompt = self.orig_all_negative_prompt[0]
                p.all_negative_prompts[self.imgcount] = self.orig_all_negative_prompt[self.imgcount] 
            self.imgcount += 1
        p.extra_generation_params["Regional Prompter"] = f"mode:{mode},divide ratio : {aratios}, Use base : {usebase}, Base ratio : {bratios}, Use common : {usecom}, Use N-common : {usencom}"
        return p


    def postprocess(self, p, processed, *args):
        if hasattr(self,"handle"):
            hook_forwards(self, p.sd_model.model.diffusion_model, remove=True)
            del self.handle

        with open(os.path.join(paths.data_path, "params.txt"), "w", encoding="utf8") as file:
            processed = Processed(p, [], p.seed, "")
            file.write(processed.infotext(p, 0))


def hook_forward(self, module):
    def forward(x, context=None, mask=None):
        if self.debug :
            print("input : ", x.size())
            print("tokens : ", context.size())
            print("module : ", module.lora_layer_name)

        height = self.h
        width = self.w

        def hr_cheker(n):
            return (n != 0) and (n & (n - 1) == 0)

        if not hr_cheker(height * width // x.size()[1]) and self.hr:
            height = self.hr_h
            width = self.hr_w

        sumer = 0
        h_states = []
        contexts = context.clone()
        # SBM Matrix mode.
        if MATMODE in self.mode:
            add = 0 # TEMP
            # Completely independent size calc.
            # Basically: sqrt(hw_ratio*x.size[1])
            # And I think shape is better than size()?  
            xs = x.size()[1]
            scale = round(math.sqrt(height*width/xs))

            dsh = round_dim(height, scale)
            dsw = round_dim(width, scale)
            
            if self.debug : print(scale,dsh,dsw,dsh*dsw,x.size()[1])
            
            # Base forward.
            cad = 0 if self.usebase else 1 # 1 * self.usebase is shorter.
            i = 0
            outb = None
            if self.usebase:
                context = contexts[:,i * TOKENS:(i + 1 + self.basebreak) * TOKENS,:]
                i = i + 1 + self.basebreak
                out = main_forward(module, x, context, mask)
                
                # if self.usebase:
                outb = out.clone()
                outb = outb.reshape(outb.size()[0], dsh, dsw, outb.size()[2]) 
            
            indlast = False
            sumh = 0
            for drow in self.aratios:
                v_states = []
                sumw = 0
                for dcell in drow.cols:
                    # Grabs a set of tokens depending on number of unrelated breaks.
                    context = contexts[:,i * TOKENS:(i + 1 + dcell.breaks) * TOKENS,:]
                    i = i + 1 + dcell.breaks
                    # if i >= contexts.size()[1]: 
                    #     indlast = True
                    out = main_forward(module, x, context, mask)
                    
                    # Actual matrix split by region.
                    
                    out = out.reshape(out.size()[0], dsh, dsw, out.size()[2]) # convert to main shape. 
                    sumw = sumw + int(dsw*dcell.ed) - int(dsw*dcell.st)
                    # if indlast:
                    addh = 0
                    addw = 0
                    if dcell.ed >= 0.999:
                        addw = sumw - dsw
                        sumh = sumh + int(dsh*drow.ed) - int(dsh*drow.st)
                        if drow.ed >= 0.999:
                            addh = sumh - dsh
                    
                    out = out[:,int(dsh*drow.st) + addh:int(dsh*drow.ed),
                              int(dsw*dcell.st) + addw:int(dsw*dcell.ed),:]
                    if self.debug : print(f"sumer:{sumer},dsw:{dsw},add:{add}")
                    if self.usebase : 
                        # outb_t = outb[:,:,int(dsw*drow.st):int(dsw*drow.ed),:].clone()
                        outb_t = outb[:,int(dsh*drow.st) + addh:int(dsh*drow.ed),
                                      int(dsw*dcell.st) + addw:int(dsw*dcell.ed),:].clone()
                        out = out * (1 - dcell.base) + outb_t * dcell.base
            
                    v_states.append(out)
                    if self.debug : 
                        for h in v_states:
                            print(h.size())
                            
                ox = torch.cat(v_states,dim = 2) # First concat the cells to rows.
                h_states.append(ox)
            ox = torch.cat(h_states,dim = 1) # Second, concat rows to layer.
            ox = ox.reshape(x.size()[0],x.size()[1],x.size()[2]) # Restore to 3d source.  
        else: # Regular handle.
            def separatecalc(x, contexts, mask, pn,divide):
                sumer = 0
                h_states = []
    
                tll = self.pt if pn else self.nt
                if self.debug : print(f"tokens : {tll},pn : {pn}")
    
                for i, tl in enumerate(tll):
                    context = contexts[:, tl[0] * 77 : tl[1] * 77, :]
                    if self.debug : print(f"tokens : {tl[0]*77}-{tl[1]*77}")
    
                    if self.usebase:
                        if i != 0:
                            area = self.aratios[i - 1]
                            bweight = self.bratios[i - 1]
                    else:
                        area = self.aratios[i]
    
                    h = module.heads // divide
                    q = module.to_q(x)
    
                    context = atm.default(context, x)
                    k = module.to_k(context)
                    v = module.to_v(context)
    
                    q, k, v = map(lambda t: atm.rearrange(t, "b n (h d) -> (b h) n d", h=h), (q, k, v))
    
                    sim = atm.einsum("b i d, b j d -> b i j", q, k) * module.scale
    
                    if atm.exists(mask):
                        mask = atm.rearrange(mask, "b ... -> b (...)")
                        max_neg_value = -torch.finfo(sim.dtype).max
                        mask = atm.repeat(mask, "b j -> (b h) () j", h=h)
                        sim.masked_fill_(~mask, max_neg_value)
    
                    attn = sim.softmax(dim=-1)
    
                    out = atm.einsum("b i j, b j d -> b i d", attn, v)
                    out = atm.rearrange(out, "(b h) n d -> b n (h d)", h=h)
                    out = module.to_out(out)
    
                    if len(self.nt) == 1 and not pn:
                        if self.debug : print("return out for NP")
                        return out
    
                    xs = x.size()[1]
                    scale = round(math.sqrt(height * width / xs))
    
                    dsh = round(height / scale)
                    dsw = round(width / scale)
                    ha, wa = xs % dsh, xs % dsw
                    if ha == 0:
                        dsw = int(xs / dsh)
                    elif wa == 0:
                        dsh = int(xs / dsw)
    
                    if self.debug : print(scale, dsh, dsw, dsh * dsw, x.size()[1])
    
                    if i == 0 and self.usebase:
                        outb = out.clone()
                        if "Horizontal" in self.mode:
                            outb = outb.reshape(outb.size()[0], dsh, dsw, outb.size()[2])
                        continue
                    add = 0
    
                    cad = 0 if self.usebase else 1
    
                    if "Horizontal" in self.mode:
                        sumer = sumer + int(dsw * area[1]) - int(dsw * area[0])
                        if i == self.divide - cad:
                            add = sumer - dsw
                        out = out.reshape(out.size()[0], dsh, dsw, out.size()[2])
                        out = out[:, :, int(dsw * area[0] + add) : int(dsw * area[1]), :]
                        if self.debug : print(f"sumer:{sumer},dsw:{dsw},add:{add}")
                        if self.usebase:
                            outb_t = outb[:, :, int(dsw * area[0] + add) : int(dsw * area[1]), :].clone()
                            out = out * (1 - bweight) + outb_t * bweight
                    elif "Vertical" in self.mode:
                        sumer = sumer + int(dsw * dsh * area[1]) - int(dsw * dsh * area[0])
                        if i == self.divide - cad:
                            add = sumer - dsw * dsh
                        out = out[:, int(dsw * dsh * area[0] + add) : int(dsw * dsh * area[1]), :]
                        if self.debug : print(f"sumer:{sumer},dsw*dsh:{dsw*dsh},add:{add}")
                        if self.usebase:
                            outb_t = outb[:,int(dsw * dsh * area[0] + add) : int(dsw * dsh * area[1]),:,].clone()
                            out = out * (1 - bweight) + outb_t * bweight
                    h_states.append(out)
                if self.debug:
                    for h in h_states :
                        print(f"divided : {h.size()}")
    
                if "Horizontal" in self.mode:
                    ox = torch.cat(h_states, dim=2)
                    ox = ox.reshape(x.size()[0], x.size()[1], x.size()[2])
                elif "Vertical" in self.mode:
                    ox = torch.cat(h_states, dim=1)
                return ox
    
            if self.eq:
                ox = separatecalc(x, contexts, mask, True, 1)
                if self.debug : print("same token size and divisions")
            elif x.size()[0] == 1 * self.batch_size:
                ox = separatecalc(x, contexts, mask, self.pn, 1)
                if self.debug : print("different tokens size")
            else:
                px, nx = x.chunk(2)
                opx = separatecalc(px, contexts, mask, True, 2)
                onx = separatecalc(nx, contexts, mask, False, 2)
                ox = torch.cat([opx, onx])
                if self.debug : print("same token size and different divisions")
                
            self.count += 1
    
            if self.count == 16:
                self.pn = not self.pn
                self.count = 0
            if self.debug : print(f"output : {ox.size()}")
        return ox

    return forward


def hook_forwards(self, root_module: torch.nn.Module, remove=False):
    for name, module in root_module.named_modules():
        if "attn2" in name and module.__class__.__name__ == "CrossAttention":
            module.forward = hook_forward(self, module)
            if remove:
                del module.forward


def tokendealer(p):
    ppl = p.prompt.split("BREAK")
    npl = p.negative_prompt.split("BREAK")
    pt, nt, ppt, pnt = [], [], [], []

    padd = 0
    for pp in ppl:
        _, tokens = shared.sd_model.cond_stage_model.tokenize_line(pp)
        pt.append([padd, tokens // 75 + 1 + padd])
        ppt.append(tokens)
        padd = tokens // 75 + 1 + padd

    padd = 0
    for np in npl:
        _, tokens = shared.sd_model.cond_stage_model.tokenize_line(np)
        nt.append([padd, tokens // 75 + 1 + padd])
        pnt.append(tokens)
        padd = tokens // 75 + 1 + padd

    return pt, nt, ppt, pnt


def promptdealer(self, p, aratios, bratios, usebase, usecom, usencom):
    aratios = [float(a) for a in aratios.split(",")]
    aratios = [a / sum(aratios) for a in aratios]

    for i, a in enumerate(aratios):
        if i == 0:
            continue
        aratios[i] = aratios[i - 1] + a

    divide = len(aratios)
    aratios_o = [0] * divide

    for i in range(divide):
        if i == 0:
            aratios_o[i] = [0, aratios[0]]
        elif i < divide:
            aratios_o[i] = [aratios[i - 1], aratios[i]]
        else:
            aratios_o[i] = [aratios[i], ""]
    if self.debug : print("regions : ", aratios_o)

    self.aratios = aratios_o
    try:
        self.bratios = [float(b) for b in bratios.split(",")]
    except:
        self.bratios = [0]

    if divide > len(self.bratios):
        while divide >= len(self.bratios):
            self.bratios.append(self.bratios[0])

    self.divide = divide

    if usecom:
        self.orig_all_prompt = p.all_prompts
        self.prompt = p.prompt = comdealer(p.prompt)
        for pr in p.all_prompts:
            self.all_prompts.append(comdealer(pr))
        p.all_prompts = self.all_prompts

    if usencom:
        self.orig_all_negative_prompt = p.all_negative_prompts
        self.negative_prompt = p.negative_prompt = comdealer(p.negative_prompt)
        for pr in p.all_negative_prompts:
            self.all_negative_prompts.append(comdealer(pr))
        p.all_negative_prompts =self.all_negative_prompts

    return self, p


def comdealer(prompt):
    ppl = prompt.split("BREAK")
    for i in range(len(ppl)):
        if i == 0:
            continue
        ppl[i] = ppl[0] + ", " + ppl[i]
    ppl = ppl[1:]
    prompt = "BREAK ".join(ppl)
    return prompt

def savepresets(name,mode, ratios, baseratios, usebase,usecom, usencom):
    path_root = scripts.basedir()
    filepath = os.path.join(path_root,"scripts", "regional_prompter_presets.csv")
    try:
        with open(filepath,mode = 'r',encoding="utf-8") as f:
            presets = f.readlines()
            pr = f'{name},{mode},"{ratios}","{baseratios}",{str(usebase)},{str(usecom)},{str(usencom)}\n'
            written = False
            if name == "lastrun":
                for i, preset in enumerate(presets):
                    if "lastrun" in preset :
                        presets[i] = pr
                        written = True
            if not written : presets.append(pr)
        with open(filepath,mode = 'w',encoding="utf-8") as f:
            f.writelines(presets)
    except Exception as e:
        print(e)
    presets = loadpresets(filepath)
    return gr.update(choices = [pr[0] for pr in presets])

def loadpresets(filepath):
    presets = []
    try:
        with open(filepath,encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) > 5:
                    presets.append(row)
            presets = presets[1:]
    except OSError as e:
        presets=PRESETS
        print("ERROR")
        if not os.path.isfile(filepath):
            try:
                with open(filepath,mode = 'w',encoding="utf-8") as f:
                    f.writelines('"name","mode","divide ratios,"use base","baseratios","usecom","usencom"\n')
                    for pr in presets:
                        text = ",".join(pr) + "\n"
                        f.writelines(text)
            except:
                pass
    return presets
