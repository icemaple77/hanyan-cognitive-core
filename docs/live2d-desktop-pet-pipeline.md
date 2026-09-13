# 含烟 Live2D 桌宠 · 从零复现

> 一张原图 → 桌面上会动、会走路的含烟。任何人照着这份文档都能重跑一遍。
> 实现仓库:`~/workspace/HanyanOS/body/live2d`(tag `v0.1.1`~`v0.1.6`)
> Obsidian 同步版:`~/workspace/AICore/digital-human/live2d-pipeline.md`

## 铁律:不要用 AI 生成或修改含烟的图

踩过三次。连"只改姿势"的 img2img 都会把脸一起改掉,公子当场就能看出来"这不是含烟"。
**只用公子给的原图**,需要新姿势/新服装就找他要。基准图在 `RefImages/hanyan-CANON-*.png`
(不进 git,归档在 `/Volumes/ExtraSpace/archive/hanyan/live2d-20260913/`)。

身份特征:高盘发+木簪、**紫色瞳孔**、尖下巴。发色有深发和淡紫两个版本,都是她。

## 链路全景

```
公子给的原图
  ↓ rembg 抠底                              【本机】
  ↓ See-through 分层 + AI 补全被遮挡的部分     【umbrella GPU,3 分钟/张】
  ↓ 切左右腿 + 补闭眼/闭嘴差分 + 打包 PSD      【本机】
  ├→ Anime2.5DRig    表情/口型强,肢体不能弯
  └→ StretchyStudio  绑骨骼 → 胳膊腿能弯、能走路
  ↓
Electron 桌宠壳(两套渲染器可切)
```

**已废弃**:psd2live → Cubism → `.moc3`;以及自己写启发式脚本切分层
(原理上补不出被遮挡的像素,而且脸部器官判定会把额头脸颊误吞进眉毛层)。

## 环境

**本机 macOS**
```bash
pip3 install rembg onnxruntime psd-tools pillow numpy opencv-python
pip3 install mediapipe==0.10.30      # 1.0.1 会崩(Metal service unavailable)
cd ~/workspace/HanyanOS/body/live2d/pet && npm install   # Electron 33
```

**umbrella(RTX 5080 16G,平时关机)**
```bash
# WOL 唤醒,约 40 秒
python3 -c "
import socket
mac='60:cf:84:cc:3a:7e'.replace(':','')
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
s.sendto(bytes.fromhex('FF'*6+mac*16),('192.168.1.255',9))"
# 用完一定关:ssh umbrella "sudo shutdown -h now"
```

See-through 一次性安装:
```bash
cd ~/workspace/projects && git clone --depth 1 https://github.com/shitagaki-lab/see-through.git
cd see-through && source ~/miniconda3/etc/profile.d/conda.sh
conda create -n see_through python=3.12 -y && conda activate see_through
export PYTHONNOUSERSITE=1     # 不加会串到用户级 site-packages
pip install torch==2.8.0+cu128 torchvision==0.23.0+cu128 torchaudio==2.8.0+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt && ln -sf common/assets assets
```
显存:默认 1280/bf16 要 12–16GB,5080 的 16GB 刚好(实测峰值 14.2GB)。不够用
`--group_offload`(~10GB)或 `inference_psd_quantized.py`(NF4,~8GB)。

## 步骤

```bash
cd ~/workspace/HanyanOS/body/live2d

# 1 抠底
python3 tools/remove_bg.py RefImages/<原图>.png assets/canon/nobg/<名字>.png

# 2 分层(远程 GPU)
scp assets/canon/nobg/<名字>.png umbrella:~/workspace/projects/see-through/
ssh umbrella "source ~/miniconda3/etc/profile.d/conda.sh && conda activate see_through && \
  export PYTHONNOUSERSITE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && \
  cd ~/workspace/projects/see-through && \
  nohup python inference/scripts/inference_psd.py --srcp <名字>.png --save_to_psd > /tmp/st.log 2>&1 &"
# 等日志出现 "psd saved to ..."
mkdir -p assets/canon/layers-<名字>
scp umbrella:"~/workspace/projects/see-through/workspace/layerdiff_output/<名字>/*.png" \
    assets/canon/layers-<名字>/

# 3 切左右腿(双腿并拢的站姿必须切,否则走不了路)+ 补差分层 + 打包
python3 tools/split-legs.py    assets/canon/layers-<名字>
python3 tools/make-eye-close.py assets/canon/layers-<名字>
python3 tools/build-a25d-psd.py assets/canon/layers-<名字> assets/canon/<名字>-a25d.psd

# 4A 表情路线
cp assets/canon/<名字>-a25d.psd pet/renderer/a25d/model.psd && cd pet && npm start

# 4B 肢体路线:先在 StretchyStudio 里绑骨骼
git clone --depth 1 https://github.com/MangoLion/stretchystudio.git
cd stretchystudio && npm install && npm run dev     # localhost:5173
#   浏览器:拖入 PSD → Continue(勾 Split merged parts + Mesh all parts)
#         → Next: Adjust Joints → **AI Auto-Rig (DWPose) → Download 50MB 模型**(不能跳)
#         → Next: Setup Parameters → Done → Save project → Download File 得到 .stretch
./node_modules/.bin/esbuild src/hanyan-player.js --bundle --format=iife \
  --outfile=~/workspace/HanyanOS/body/live2d/pet/renderer/stretch/hanyan-player.js
cp <导出>.stretch ~/workspace/HanyanOS/body/live2d/pet/renderer/stretch/model.stretch
cd ~/workspace/HanyanOS/body/live2d/pet && HANYAN_PET_RENDERER=stretch npm start
```

开关:`HANYAN_PET_RENDERER=stretch`(能弯肢体)、`HANYAN_PET_MODEL=<id>`(开机服装)、
`HANYAN_PET_ROAM=1`(到处走,默认站着)、`HANYAN_PET_FREEZE=1`(调试冻结)。
右键角色:换装 / 自由走动 / 心情 / 退出;`Cmd+Shift+Q` 强退。

## 坑(全是实际踩过的)

| # | 症状 | 正解 |
|---|---|---|
| 1 | 脸变了 | 别用 AI 生成/改图,见开头铁律 |
| 2 | 一动就露洞、`face.png` 只剩一条鼻嘴窄带 | 别自己写启发式分层,用 See-through |
| 3 | **嘴没了** | See-through 的 `mouth` 被 Anime2.5DRig 当成"张开的嘴",要补 `mouth_close`(build-a25d-psd.py 自动做) |
| 4 | 闭眼是白条/肤色补丁 | 用 `make-eye-close.py` 从角色自己的素材合成(眼眶 inpaint + 自己的睫毛压扁) |
| 5 | **转胳膊时整条手臂飞出去** | StretchyStudio 里必须跑 DWPose,否则支点在图层中心不在关节 |
| 6 | 手臂参数拉满没反应 | 自动生成的 41 个参数只有 12 个真绑了形变;绕过参数直接写 `Map<骨骼id,{rotation}>` |
| 7 | 想用 Spine 导出 | Spine 运行时**收费授权**(每个使用者都要买 Spine Editor),用剥出来的 StretchyStudio 运行时(MIT) |
| 8 | 眨眼糊 | 全身图脸只有 115px;对头肩特写单独跑一遍再用 `graft-hires-face.py` 移植回来 |
| 9 | 脖子挡住挂脖裙肩带 | 层序 `neck` 要在 `topwear` 下面 |
| 10 | Electron 加载 ES 模块被 CORS 挡 | esbuild 打成 IIFE 单文件 |
| 11 | 走路时鞋子脱节 | 交叉站姿的原图左右腿重叠、鞋归错腿。**用 T-pose 原图绑定**(四肢完全分开),显示时用 `setRestPose('tpose')` 把胳膊转下来 |
| 12 | 腿变成一个 `bothLegs` 节点,走不了路 | 双腿并拢时连通域分不开左右,先跑 `tools/split-legs.py` 沿中线竖直切(只对双腿平行的站姿有效,交叉站姿切了会归错) |
| 13 | **转头时头从脖子上飞出去** | DWPose 不给 head 设支点,导出默认值 (640,1280) 在画布底边。加载时检测到支点在底部就挪到 neck 支点(播放器 `fixHeadPivot`) |
| 14 | GPT 改衣服后脸漂了 | 开了 `input_fidelity: "high"` 也会漂。先出一张给公子核对;补救用 `graft-hires-face.py` 把原图脸部图层移植过去,不用再花钱重生成 |

**关键认识**:绑定姿势和显示姿势不是同一个。T-pose 是最好的绑定素材(四肢不重叠、
分层最干净),绑完在运行时把胳膊转下来即可。

## 能动 / 不能动

能动:头部伪 3D 转动+歪头、视线跟随、眨眼、眉毛、口型、头发多束物理、呼吸、身体倾斜;
stretch 运行时还能弯胳膊/手肘/膝盖、走路(迈腿+屈膝+手臂反摆+身体起伏)。

不能动:手指。天花板是 2.5D 网格变形——商用 VTuber 级别要人工在 Cubism Editor 里
逐个建变形器,一个模型几天到几周。

## 上游

- See-through https://github.com/shitagaki-lab/see-through (SIGGRAPH 2026)
- Anime2.5DRig https://github.com/852wa/Anime2.5DRig (MIT)
- StretchyStudio https://github.com/MangoLion/stretchystudio (MIT)
