# -*- coding: utf-8 -*-
# Copyright (c) 2022 Salvador E. Tropea
# Copyright (c) 2022 Instituto Nacional de Tecnología Industrial
# License: GPL-3.0
# Project: KiBot (formerly KiPlot)
"""
Dependencies:
  - name: markdown2
    python_module: true
    debian: python3-markdown2
    arch: python-markdown2
    role: mandatory
"""
import glob
import os
import shutil
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from .error import KiPlotConfigurationError
from .misc import W_PCBDRAW, RENDERERS, PCB_GENERATORS, W_MORERES
from .gs import GS
from .kiplot import config_output, run_output, get_output_dir, load_board, run_command
from .optionable import BaseOptions, Optionable
from .registrable import RegOutput
from .PcbDraw.present import boardpage, readTemplate
from .macros import macros, document, output_class  # noqa: F401
from . import log


logger = log.get_logger()


def pcbdraw_warnings(tag, msg):
    logger.warning('{}({}) {}'.format(W_PCBDRAW, tag, msg))


def _get_tmp_name(ext):
    with NamedTemporaryFile(mode='w', suffix='.'+ext, delete=False) as f:
        f.close()
    return f.name


class PresentBoards(Optionable):
    def __init__(self):
        super().__init__()
        with document:
            self.mode = 'local'
            """ [local,file,external] How images and gerbers are obtained.
                *local*: Only applies to the currently selected PCB.
                You must provide the names of the outputs used to render
                the images and compress the gerbers.
                When empty KiBot will use the first render/gerber output
                it finds.
                To apply variants use `pcb_from_output` and a `pcb_variant`
                output.
                *file*: You must specify the file names used for the images and
                the gerbers.
                *external*: You must specify an external KiBot configuration.
                It will be applied to the selected PCB to create the images and
                the gerbers. The front image must be generated in a dir called
                *front*, the back image in a dir called *back* and the gerbers
                in a dir called *gerbers* """
            self.name = ''
            """ *Name for this board. If empty we use the name of the PCB.
                Applies to all modes """
            self.comment = ''
            """ A comment or description for this board.
                Applies to all modes """
            self.pcb_file = ''
            """ Name of the KiCad PCB file. When empty we use the current PCB.
                Is ignored for the *local* mode """
            self.pcb_from_output = ''
            """ Use the PCB generated by another output.
                Is ignored for the *file* mode """
            self.front_image = ''
            """ *local*: the name of an output that renders the front.
                If empty we use the first renderer for the front.
                *file*: the name of the rendered image.
                *external*: ignored, we use `extrenal_config` """
            self.back_image = ''
            """ *local*: the name of an output that renders the back.
                If empty we use the first renderer for the back.
                *file*: the name of the rendered image.
                *external*: ignored, we use `extrenal_config` """
            self.gerbers = ''
            """ *local*: the name of a `gerber` output.
                If empty we use the first `gerber` output.
                *file*: the name of a compressed archive.
                *external*: ignored, we use `extrenal_config` """
            self.external_config = ''
            """ Name of an external KiBot configuration.
                Only used in the *external* mode """

    def config(self, parent):
        super().config(parent)
        if not self.name:
            self.name = GS.pcb_basename
        if not self.pcb_file:
            self.pcb_file = GS.pcb_file
        if self.mode == 'file':
            files = [self.front_image, self.back_image, self.gerbers]
            for f in files:
                if not os.path.isfile(f):
                    raise KiPlotConfigurationError('Missing file: `{}`'.format(f))
        elif self.mode == 'external':
            if not self.external_config:
                raise KiPlotConfigurationError('`external_config` must be specified for the external mode')
            if not os.path.isfile(self.external_config):
                raise KiPlotConfigurationError('Missing external config: `{}`'.format(self.external_config))
        if self.pcb_from_output and self.mode != 'file':
            out = RegOutput.get_output(self.pcb_from_output)
            self._pcb_from_output = out
            if out is None:
                raise KiPlotConfigurationError('Unknown output `{}` selected in board {}'.
                                               format(self.pcb_from_output, self.name))

    def solve_file(self):
        return self.name, self.comment, self.pcb_file, self.front_image, self.back_image, self.gerbers

    def generate_image(self, back, tmp_name):
        self.save_renderer_options()
        options = self._renderer.options
        logger.debug('Starting renderer with back: {}, name: {}'.format(back, tmp_name))
        # Configure it according to our needs
        options._filters_to_expand = False
        options.show_components = None if self._renderer_is_pcbdraw else []
        options.highlight = []
        options.output = tmp_name
        self._renderer._done = False
        if self._renderer_is_pcbdraw:
            options.add_to_variant = False
            options.bottom = back
        else:  # render_3D
            options.view = 'Z' if back else 'z'
            options._show_all_components = False
        run_output(self._renderer)
        self.restore_renderer_options()

    def do_compress(self, tmp_name, out):
        tree = {'name': '_temporal_compress_gerbers',
                'type': 'compress',
                'comment': 'Internally created to compress gerbers',
                'options': {'output': tmp_name, 'files': [{'from_output': out.name, 'dest': '/'}]}}
        out = RegOutput.get_class_for('compress')()
        out.set_tree(tree)
        config_output(out)
        logger.debug('Creating gerbers archive ...')
        out.options.run(tmp_name)

    def generate_archive(self, out, tmp_name):
        out.options
        logger.debug('Starting gerber name: {}'.format(out.name))
        # Save options
        old_dir = out.dir
        old_done = out._done
        old_variant = out.options.variant
        # Configure it according to our needs
        with TemporaryDirectory() as tmp_dir:
            logger.debug('Generating the gerbers at '+tmp_dir)
            out.done = False
            out.dir = tmp_dir
            out.options.variant = None
            run_output(out)
            self.do_compress(tmp_name, out)
        # Restore options
        out.dir = old_dir
        out._done = old_done
        out.options.variant = old_variant

    def solve_pcb(self, load_it=True):
        if not self.pcb_from_output:
            return False
        out_name = self.pcb_from_output
        out = RegOutput.get_output(out_name)
        if out is None:
            raise KiPlotConfigurationError('Unknown output `{}` selected in board {}'. format(out_name, self.name))
        if out.type not in PCB_GENERATORS:
            raise KiPlotConfigurationError("Output `{}` can't be used to render the PCB, must be {}".
                                           format(out, PCB_GENERATORS))
        config_output(out)
        run_output(out)
        new_pcb = out.get_targets(get_output_dir(out.dir, out))[0]
        if load_it:
            GS.board = None
            self.old_pcb = GS.pcb_file
            GS.set_pcb(new_pcb)
            load_board()
        self.new_pcb = new_pcb
        return True

    def solve_local_image(self, out_name, back=False):
        if not out_name:
            out = next(filter(lambda x: x.type in RENDERERS, RegOutput.get_outputs()), None)
            if not out:
                raise KiPlotConfigurationError('No renderer output found, must be {}'.format(RENDERERS))
        else:
            out = RegOutput.get_output(out_name)
            if out is None:
                raise KiPlotConfigurationError('Unknown output `{}` selected in board {}'. format(out_name, self.name))
            if out.type not in RENDERERS:
                raise KiPlotConfigurationError("Output `{}` can't be used to render the PCB, must be {}".
                                               format(out, RENDERERS))
        config_output(out)
        self._renderer = out
        self._renderer_is_pcbdraw = out.type == 'pcbdraw'
        tmp_name = _get_tmp_name(out.get_extension())
        self.temporals.append(tmp_name)
        self.generate_image(back, tmp_name)
        return tmp_name

    def solve_local_gerbers(self, out_name):
        if not out_name:
            out = next(filter(lambda x: x.type == 'gerber', RegOutput.get_outputs()), None)
            if not out:
                raise KiPlotConfigurationError('No gerber output found')
        else:
            out = RegOutput.get_output(out_name)
            if out is None:
                raise KiPlotConfigurationError('Unknown output `{}` selected in board {}'. format(out_name, self.name))
            if out.type != 'gerber':
                raise KiPlotConfigurationError("Output `{}` must be `gerber` type, not {}". format(out, out.type))
        config_output(out)
        tmp_name = _get_tmp_name('zip')
        self.temporals.append(tmp_name)
        # Generate the archive
        self.generate_archive(out, tmp_name)
        return tmp_name

    def solve_local(self):
        fname = GS.pcb_file
        pcb_changed = self.solve_pcb()
        front_image = self.solve_local_image(self.front_image)
        back_image = self.solve_local_image(self.back_image, back=True)
        gerbers = self.solve_local_gerbers(self.gerbers)
        if pcb_changed:
            fname = self.new_pcb
            GS.set_pcb(self.old_pcb)
            GS.reload_project(GS.pro_file)
        return self.name, self.comment, fname, front_image, back_image, gerbers

    def get_ext_file(self, main_dir, sub_dir):
        d_name = os.path.join(main_dir, sub_dir)
        logger.debugl(1, 'Looking for results at '+d_name)
        if not os.path.isdir(d_name):
            raise KiPlotConfigurationError('`{}` should create a directory called `{}`'.
                                           format(self.external_config, sub_dir))
        res = glob.glob(os.path.join(d_name, '*'))
        if not res:
            raise KiPlotConfigurationError('`{}` created an empty `{}`'.
                                           format(self.external_config, sub_dir))
        if len(res) > 1:
            logger.warning(W_MORERES+'`{}` generated more than one file at `{}`'.
                           format(self.external_config, sub_dir))
        return res[0]

    def solve_external(self):
        tmp_dir = mkdtemp()
        self.temporals.append(tmp_dir)
        fname = self.new_pcb if self.solve_pcb(load_it=False) else GS.pcb_file
        cmd = [sys.argv[0], '-c', self.external_config, '-b', fname, '-d', tmp_dir]
        run_command(cmd)
        front_image = self.get_ext_file(tmp_dir, 'front')
        back_image = self.get_ext_file(tmp_dir, 'back')
        gerbers = self.get_ext_file(tmp_dir, 'gerbers')
        return self.name, self.comment, fname, front_image, back_image, gerbers

    def solve(self, temporals):
        self.temporals = temporals
        if self.mode == 'file':
            return self.solve_file()
        elif self.mode == 'local':
            return self.solve_local()
        # external
        return self.solve_external()


class KiKit_PresentOptions(BaseOptions):
    def __init__(self):
        with document:
            self.description = ''
            """ *Name for a markdown file containing the main part of the page to be generated.
                This is mandatory and is the description of your project """
            self.boards = PresentBoards
            """ [dict|list(dict)] One or more boards that compose your project.
                When empty we will use only the main PCB for the current project """
            self.resources = Optionable
            """ [string|list(string)='']  A list of file name patterns for additional resources to be included.
                I.e. images referenced in description.
                They will be copied relative to the output dir """
            self.template = 'default'
            """ Path to a template directory or a name of built-in one.
                See KiKit's doc/present.md for template specification """
            self.repository = ''
            """ URL of the repository. Will be passed to the template """
            self.name = ''
            """ Name of the project. Will be passed to the template.
                If empty we use the name of the KiCad project.
                The default template uses it for things like the page title """
        super().__init__()

    def config(self, parent):
        super().config(parent)
        # Validate the input file name
        if not self.description:
            raise KiPlotConfigurationError('You must specify an input markdown file using `description`')
        if not os.path.isfile(self.description):
            raise KiPlotConfigurationError('Missing description file `{}`'.format(self.description))
        # List of boards
        if isinstance(self.boards, type):
            a_board = PresentBoards()
            a_board.fill_empty_values()
            self.boards = [a_board]
        elif isinstance(self.boards, PresentBoards):
            self.boards = [self.boards]
        # else ... we have a list of boards
        self.resources = self.force_list(self.resources, comma_sep=False)
        if not self.name:
            self.name = GS.pcb_basename
        # Make sure the template exists
        if not os.path.exists(os.path.join(self.template, "template.json")):
            try:
                self.template = GS.get_resource_path(os.path.join('pcbdraw', 'present', 'templates', self.template))
            except SystemExit:
                raise KiPlotConfigurationError('Missing template `{}`'.format(self.template))

    def get_targets(self, out_dir):
        # The web page
        out_dir = self._parent.expand_dirname(out_dir)
        res = [os.path.join(out_dir, 'index.html')]
        # The resources
        template = readTemplate(self.template)
        for r in self.resources:
            template.addResource(r)
        res.extend(template.listResources(out_dir))
        # The boards
        res.append(os.path.join(out_dir, 'boards'))
        return res

    def generate_images(self, dir_name, content):
        # Memorize the current options
        self.save_options()
        dir = os.path.dirname(os.path.join(dir_name, self.imgname))
        if not os.path.exists(dir):
            os.makedirs(dir)
        counter = 0
        for item in content:
            if item["type"] != "steps":
                continue
            for x in item["steps"]:
                counter += 1
                filename = self.imgname.replace('%d', str(counter))
                x["img"] = self.generate_image(x["side"], x["components"], x["active_components"], filename)
        # Restore the options
        self.restore_options()
        return content

    def run(self, dir_name):
        # Generate missing images
        board = []
        temporals = []
        try:
            for brd in self.boards:
                board.append(brd.solve(temporals))
            try:
                boardpage(dir_name, self.description, board, self.resources, self.template, self.repository, self.name)
            except RuntimeError as e:
                raise KiPlotConfigurationError('KiKit present error: '+str(e))
        finally:
            for f in temporals:
                if os.path.isfile(f):
                    os.remove(f)
                elif os.path.isdir(f):
                    shutil.rmtree(f)


@output_class
class KiKit_Present(BaseOutput):  # noqa: F821
    """ KiKit's Present - Project Presentation
        Creates an HTML file showing your project.
        It can contain one or more PCBs, showing their top and bottom sides.
        Also includes a download link and the gerbers. """
    def __init__(self):
        super().__init__()
        with document:
            self.options = KiKit_PresentOptions
            """ *[dict] Options for the `kikit_present` output """
        self._category = 'PCB/docs'
