import QtQuick

// Scene-graph version of the neural HUD. The geometry is generated once and
// animated with retained Qt Quick items, so the Windows scene graph can render
// it through its D3D11 backend without rebuilding the field on every frame.
Item {
    id: root
    objectName: "neuralGpuRoot"
    property int mode: 0
    property string activityState: "STANDBY"
    property real energy: 0.12
    property real amplitude: 0.0
    property int callCount: 0
    property real phase: 0.0
    property bool active: true
    property var segments: []
    property var glowSegments: []
    property var points: []
    property var sparks: []
    property var regions: []
    readonly property var colorPalette: ["#A84DFF", "#FF35A8", "#FFC13D", "#45E8FF",
                                         "#FF3B50", "#66FF42", "#792DFF", "#F8FBFF"]
    readonly property bool thinking: activityState === "THINKING" || activityState === "PROCESSING"
    readonly property real motion: (thinking ? 1.0 : 0.48) + amplitude * 0.38

    function randomSource() {
        var seed = 7319;
        return function() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
    }
    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function makeField() {
        var rnd = randomSource(), orbMode = mode === 1;
        var cx = orbMode ? 0.5 : 0.47, cy = 0.49;
        var base = [["PRE-FRONTAL",0,0.43,0.12], ["MOTOR CORTEX",1,0.74,0.22],
          ["ASSOCIATION",6,0.19,0.28], ["CONCEPT LAYER",2,0.67,0.88],
          ["SENSORY CORTEX",3,0.84,0.49], ["LANGUAGE",4,0.68,0.70],
          ["HIPPOCAMPUS",5,0.43,0.88]], seg = [], glow = [], pts = [], moving = [], tags = [];

        function line(x1, y1, x2, y2, color, power, pulse, bright) {
            var item = { x1:clamp(x1,0.006,0.994), y1:clamp(y1,0.025,0.975),
                         x2:clamp(x2,0.006,0.994), y2:clamp(y2,0.025,0.975),
                         color:color, power:power, pulse:pulse };
            seg.push(item);
            if (bright && glow.length < 190) glow.push(item);
            if (moving.length < 54 && rnd() < 0.23)
                moving.push({ x1:item.x1, y1:item.y1, x2:item.x2, y2:item.y2,
                              color:color, pulse:rnd()*6.283, speed:0.55+rnd()*1.2, size:1.2+rnd()*1.5 });
        }
        // Short curved polylines create organic axon-like paths from low-cost
        // rectangles. Side branches reuse the same routine at smaller scales.
        var regionSegStart = 0;
        function filament(x, y, heading, length, depth, color, power) {
            // Give every colored task region its own geometry budget. A single
            // global cap made the first clusters consume the whole field.
            if (depth <= 0 || seg.length-regionSegStart >= 196) return;
            var scale = orbMode ? 0.60 : 1.0;
            var bend = (rnd()-0.5) * 0.8;
            var wave = (rnd()-0.5) * length * 0.50;
            var ex = x + Math.cos(heading) * length * scale;
            var ey = y + Math.sin(heading) * length * scale;
            var c1x=x+(ex-x)*0.32-Math.sin(heading)*wave;
            var c1y=y+(ey-y)*0.32+Math.cos(heading)*wave;
            var c2x=x+(ex-x)*0.68-Math.sin(heading)*wave*(1+bend*0.5);
            var c2y=y+(ey-y)*0.68+Math.cos(heading)*wave*(1+bend*0.5);
            var midX=0.125*x+0.375*c1x+0.375*c2x+0.125*ex;
            var midY=0.125*y+0.375*c1y+0.375*c2y+0.125*ey;
            var px=x, py=y, pulse=rnd()*6.283;
            var bright=power>0.48 && rnd()<0.40;
            // Three GPU line segments approximate a smooth Bezier fiber while
            // retaining Qt Quick's batched scene-graph rendering.
            for(var step=1;step<=3;step++) {
                var t=step/3, omt=1-t;
                var nx=omt*omt*omt*x+3*omt*omt*t*c1x+3*omt*t*t*c2x+t*t*t*ex;
                var ny=omt*omt*omt*y+3*omt*omt*t*c1y+3*omt*t*t*c2y+t*t*t*ey;
                line(px,py,nx,ny,color,power*(1-(step-1)*0.045),pulse+step*0.24,
                     bright && rnd()<0.88);
                px=nx; py=ny;
            }
            if (rnd() < 0.68)
                pts.push({x:ex,y:ey,color:color,size:0.55+rnd()*1.35,pulse:pulse});
            if (depth > 1) {
                var split = 0.32+rnd()*0.39;
                filament(ex,ey,heading-bend-split,length*0.69,depth-1,color,power*0.88);
                filament(ex,ey,heading-bend+split,length*0.69,depth-1,color,power*0.88);
                if (depth > 2 && rnd() > 0.62)
                    filament(midX,midY,heading+(rnd()-0.5)*0.65,length*0.50,depth-1,color,power*0.70);
            }
        }

        // Soft nebula pools behind each functional cluster.
        for (var i=0; i<base.length; i++) {
            var b=base[i], a=i/7*6.283-1.42;
            var rx=orbMode ? cx+Math.cos(a)*0.39 : b[2];
            var ry=orbMode ? cy+Math.sin(a)*0.39 : b[3];
            tags.push({title:b[0],color:b[1],x:rx,y:ry,nodes:orbMode?60:96});
            var head=Math.atan2(ry-cy,rx-cx);
            regionSegStart=seg.length;
            var roots=orbMode?5:7;
            for (var tr=0; tr<roots; tr++) {
                var spread=(rnd()-0.5)*(orbMode?1.05:1.48);
                var sx=cx+(rx-cx)*(0.12+rnd()*0.20), sy=cy+(ry-cy)*(0.12+rnd()*0.20);
                filament(sx,sy,head+spread,orbMode?0.074:0.105,orbMode?4:5,b[1],0.52+rnd()*0.44);
            }
            // Cluster-local mesh of synapses, with dense short links plus a few
            // long cross-cluster connections to make the field span the display.
            var local=[];
            for (var n=0;n<52;n++) {
                var px=clamp(rx+(rnd()-0.5)*(orbMode?0.19:0.30),0.015,0.985);
                var py=clamp(ry+(rnd()-0.5)*(orbMode?0.18:0.27),0.035,0.965);
                local.push({x:px,y:py});
                if (pts.length<580) pts.push({x:px,y:py,color:b[1],size:0.45+rnd()*1.35,pulse:rnd()*6.283});
            }
            for (var e=0;e<24;e++) {
                var u=local[Math.floor(rnd()*local.length)], v=local[Math.floor(rnd()*local.length)];
                line(u.x,u.y,v.x,v.y,b[1],0.20+rnd()*0.35,rnd()*6.283,rnd()<0.09);
            }
        }
        for (var j=0;j<tags.length;j++) {
            var s=tags[j], t=tags[(j+2)%tags.length];
            for (var k=0;k<5;k++) {
                var qx=s.x+(rnd()-0.5)*0.18, qy=s.y+(rnd()-0.5)*0.15;
                var tx=t.x+(rnd()-0.5)*0.18, ty=t.y+(rnd()-0.5)*0.15;
                line(qx,qy,tx,ty,s.color,0.24+rnd()*0.38,rnd()*6.283,rnd()<0.18);
            }
        }
        // Long radial axons, some deliberately reach the screen boundaries.
        var rays=orbMode?24:36;
        for (var r=0;r<rays;r++) {
            var h=r/rays*6.283+(rnd()-0.5)*0.14;
            var reach=orbMode?0.40:0.70+rnd()*0.20;
            var edgeX=clamp(cx+Math.cos(h)*reach,0.004,0.996);
            var edgeY=clamp(cy+Math.sin(h)*reach,0.025,0.975);
            line(cx,cy,edgeX,edgeY,Math.floor(rnd()*7),0.20+rnd()*0.28,rnd()*6.283,rnd()<0.38);
        }
        for (var st=0;st<175;st++)
            pts.push({x:rnd(),y:rnd(),color:Math.floor(rnd()*7),size:0.28+rnd()*0.62,pulse:rnd()*6.283,star:true});
        segments=seg; glowSegments=glow; points=pts; sparks=moving; regions=tags;
    }

    Timer {
        // Animate at display cadence; geometry is retained by Qt Quick and
        // submitted through the native GPU scene graph at full resolution.
        interval: 16; running: root.active; repeat: true
        onTriggered: root.phase += (root.thinking ? 0.075 : 0.032) + root.amplitude*0.028
    }
    onModeChanged: makeField()
    Component.onCompleted: makeField()

    Rectangle { anchors.fill:parent; color:"#040610" }
    // Deep color wash, like the reference's purple/blue nebula rather than a
    // flat black canvas. Large circles remain simple GPU scene-graph geometry.
    Rectangle { width:parent.width*0.82;height:width;radius:width/2;x:parent.width*0.12-width/2;y:parent.height*0.50-height/2;color:"#542080";opacity:0.075+root.energy*0.035 }
    Rectangle { width:parent.width*0.55;height:width;radius:width/2;x:parent.width*0.76-width/2;y:parent.height*0.44-height/2;color:"#073B60";opacity:0.055+root.energy*0.025 }

    // Axon haze underneath the fine bright filaments.
    Repeater { model:root.glowSegments; delegate:Rectangle {
        required property var modelData
        property real dx:(modelData.x2-modelData.x1)*root.width
        property real dy:(modelData.y2-modelData.y1)*root.height
        x:modelData.x1*root.width; y:modelData.y1*root.height-2.0
        width:Math.sqrt(dx*dx+dy*dy); height:4.2+root.amplitude*1.2; radius:height/2
        transformOrigin:Item.Left; rotation:Math.atan2(dy,dx)*180/Math.PI
        color:root.colorPalette[modelData.color]
        opacity:0.12*modelData.power*(0.62+0.38*Math.sin(root.phase+modelData.pulse))*root.motion
    } }
    Repeater { model:root.segments; delegate:Rectangle {
        required property var modelData
        property real dx:(modelData.x2-modelData.x1)*root.width
        property real dy:(modelData.y2-modelData.y1)*root.height
        x:modelData.x1*root.width; y:modelData.y1*root.height-0.55
        width:Math.sqrt(dx*dx+dy*dy); height:1.0+root.amplitude*0.35; radius:height/2
        transformOrigin:Item.Left; rotation:Math.atan2(dy,dx)*180/Math.PI
        color:root.colorPalette[modelData.color]
        opacity:modelData.power*(0.22+0.34*root.energy+0.24*Math.sin(root.phase*1.15+modelData.pulse))*root.motion
    } }
    // Short moving packets of light travel down real synapse connections.
    Repeater { model:root.sparks; delegate:Item {
        required property var modelData
        property real travel:(root.phase*modelData.speed+modelData.pulse)%1
        property real px:(modelData.x1+(modelData.x2-modelData.x1)*travel)*root.width
        property real py:(modelData.y1+(modelData.y2-modelData.y1)*travel)*root.height
        x:px-modelData.size*3; y:py-modelData.size*3; width:modelData.size*6; height:width
        Rectangle { anchors.fill:parent;radius:width/2;color:root.colorPalette[parent.modelData.color];opacity:0.18+root.energy*0.20 }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.28;height:width;radius:width/2;color:"#FFFFFF";opacity:0.95 }
    } }
    Repeater { model:root.points; delegate:Rectangle {
        required property var modelData
        width:modelData.size*(modelData.star?2.8:2.5);height:width;radius:width/2
        x:modelData.x*root.width-width/2;y:modelData.y*root.height-height/2
        color:modelData.star?"#DCD8F3":root.colorPalette[modelData.color]
        opacity:(modelData.star?0.14:0.23)*(0.45+0.55*Math.abs(Math.sin(root.phase+modelData.pulse)))
    } }
    Repeater { model:root.points; delegate:Rectangle {
        required property var modelData
        width:modelData.size*(modelData.star?0.9:1.15);height:width;radius:width/2
        x:modelData.x*root.width-width/2;y:modelData.y*root.height-height/2
        color:modelData.star?"#F3EFFF":root.colorPalette[modelData.color]
        opacity:(modelData.star?0.40:0.84)*(0.50+0.50*Math.abs(Math.sin(root.phase*1.35+modelData.pulse)))
    } }

    // The Synapse Field's nucleus and radial flare are visible in both modes;
    // in Neural Orb mode the spherical core replaces the large field nucleus.
    Item {
        id:nucleus; visible:root.mode===0; z:4
        property real diameter:Math.min(root.width,root.height)*(0.075+root.energy*0.025)
        width:diameter;height:diameter;x:root.width*0.47-width/2;y:root.height*0.49-height/2
        Rectangle { anchors.centerIn:parent;width:parent.width*7.0;height:width;radius:width/2;color:"#923BFF";opacity:0.055+root.energy*0.035 }
        Rectangle { anchors.centerIn:parent;width:parent.width*4.5;height:width;radius:width/2;color:"#E03DFF";opacity:0.075+root.energy*0.045 }
        Rectangle { anchors.centerIn:parent;width:parent.width*2.0;height:width;radius:width/2;color:"#A7E9FF";opacity:0.13+root.energy*0.08 }
        Repeater { model:24;delegate:Rectangle {
            id:flareRay
            required property int index
            property real angle:index/24*360
            width:parent.width*(2.8+(index%4)*0.45);height:index%3===0?1.4:0.7
            x:parent.width/2;y:parent.height/2-height/2
            transform:Rotation { origin.x:0;origin.y:flareRay.height/2;angle:flareRay.angle }
            color:index%3===0?"#E5B5FF":"#8BEAFF"
            opacity:(index%3===0?0.38:0.22)*(0.65+0.35*Math.sin(root.phase*1.8+index))
        } }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.72;height:width;radius:width/2
            gradient:Gradient { GradientStop{position:0;color:"#FFFFFF"} GradientStop{position:0.23;color:"#E7C8FF"} GradientStop{position:0.62;color:"#B550FF"} GradientStop{position:1;color:"#5F22B5"} }
            opacity:0.86+root.energy*0.12
        }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.22;height:width;radius:width/2;color:"white";opacity:0.98 }
    }

    // Detailed luminous globe: layered halo, rotating latitudes/meridians,
    // synaptic surface dots and a pulsing central light.
    Item {
        id:orb;visible:root.mode===1
        property real diameter:Math.min(root.width,root.height)*0.39
        width:diameter;height:diameter;x:(root.width-width)/2;y:(root.height-height)/2;z:5
        Rectangle { anchors.centerIn:parent;width:parent.width*2.0;height:width;radius:width/2;color:"#792DFF";opacity:0.07+root.energy*0.035 }
        Rectangle { anchors.centerIn:parent;width:parent.width*1.58;height:width;radius:width/2;color:"#B04BFF";opacity:0.085+root.energy*0.045 }
        Rectangle { anchors.centerIn:parent;width:parent.width*1.28;height:width;radius:width/2;color:"#43DFFF";opacity:0.11+root.energy*0.045 }
        Rectangle { id:globe;anchors.fill:parent;radius:width/2;border.width:1.5;border.color:"#A3F1FF";opacity:0.95
            gradient:Gradient { GradientStop{position:0;color:"#C9B5FF"} GradientStop{position:0.18;color:"#56339F"} GradientStop{position:0.57;color:"#17194C"} GradientStop{position:1;color:"#041420"} }
        }
        Item { anchors.fill:parent;clip:true
            Repeater { model:7;delegate:Rectangle {
                required property int index
                anchors.centerIn:parent;width:parent.width*(0.16+index*0.118);height:parent.height*0.94
                radius:width/2;color:"transparent";border.width:index===0?1.4:0.8
                border.color:index%2===0?"#8DEBFF":"#E275FF"
                opacity:(0.31+root.energy*0.19)*(index===0?0.78:1)
                RotationAnimation on rotation { from:0;to:360;duration:11000+index*1900;loops:Animation.Infinite }
            } }
            Repeater { model:7;delegate:Rectangle {
                required property int index
                anchors.centerIn:parent;width:parent.width*0.97;height:parent.height*(0.09+index*0.135)
                radius:height/2;color:"transparent";border.width:0.8
                border.color:index%2===0?"#72DFFF":"#D67BFF";opacity:0.25+root.energy*0.20
            } }
            Repeater { model:64;delegate:Rectangle {
                required property int index
                property real lon:index*2.3999632297
                property real lat:Math.asin(-1+2*(index+0.5)/64)
                property real rot:lon+root.phase*0.10
                property real sx:Math.cos(lat)*Math.sin(rot)
                property real sy:Math.sin(lat)
                property real depth:Math.cos(lat)*Math.cos(rot)
                property real d:1.0+Math.max(0,depth)*1.4
                width:d;height:d;x:parent.width*(0.5+sx*0.46)-width/2;y:parent.height*(0.5-sy*0.46)-height/2
                color:index%4===0?"#FFFFFF":(index%2===0?"#88F0FF":"#E68CFF")
                opacity:depth< -0.15?0.12:0.44+Math.max(0,depth)*0.54
            } }
        }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.30;height:width;radius:width/2;color:"#D950FF";opacity:0.10+root.energy*0.07 }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.14;height:width;radius:width/2
            gradient:Gradient { GradientStop{position:0;color:"#FFFFFF"} GradientStop{position:0.38;color:"#E7D5FF"} GradientStop{position:1;color:"#9D55FF"} }
            opacity:0.95+root.amplitude*0.05
        }
        Rectangle { anchors.centerIn:parent;width:parent.width*0.055;height:width;radius:width/2;color:"white";opacity:1 }
    }

    Repeater { model:root.regions;delegate:Item {
        required property var modelData
        property color accent:root.colorPalette[modelData.color]
        width:Math.min(155,root.width*0.28);height:43
        x:root.clamp(modelData.x*root.width-width/2,5,root.width-width-5)
        y:root.clamp(modelData.y*root.height-height/2,22,root.height-height-20);z:20
        Rectangle { anchors.fill:parent;color:"#080B14";opacity:0.88;border.width:1;border.color:parent.accent }
        Rectangle { x:0;y:3;width:2;height:parent.height-6;color:parent.accent }
        Text { x:9;y:5;width:parent.width-14;text:modelData.title;color:parent.accent;font.family:"Consolas";font.pixelSize:Math.max(8,root.height*0.016);font.bold:true;elide:Text.ElideRight }
        Text { x:9;y:24;width:parent.width-14;text:modelData.nodes+" nodes · signal "+Math.round(root.energy*100)+"%";color:"#A8ACBB";font.family:"Consolas";font.pixelSize:Math.max(7,root.height*0.012);elide:Text.ElideRight }
    } }
    Text { x:20;y:10;text:(root.mode===1?"NEURAL ORB":"SYNAPSE FIELD")+"  /  "+root.activityState;color:"#D6DDF2";font.family:"Consolas";font.pixelSize:Math.max(8,root.height*0.018);z:30 }
    Text { anchors.right:parent.right;anchors.rightMargin:20;y:10;text:"ACTIVITY "+Math.round(root.energy*100)+"%  ·  CALL "+("0000"+root.callCount).slice(-4);color:"#D6DDF2";font.family:"Consolas";font.pixelSize:Math.max(8,root.height*0.018);z:30 }
}
